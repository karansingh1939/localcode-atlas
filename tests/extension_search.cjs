const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const os = require('node:os');
const net = require('node:net');
const { spawn } = require('node:child_process');
const http = require('node:http');

async function checkSearch() {
    const root = process.env.CODELENS_TEST_ROOT || path.resolve(__dirname, '..');
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'codelens-integration-'));
    const repo = path.join(temp, 'repo');
    fs.mkdirSync(repo);
    const source = path.join(repo, 'parser.py');
    fs.writeFileSync(
        source,
        'def parse_code_files(source):\n    """Parse code files into chunks."""\n    return source.splitlines()\n',
    );
    const socket = net.createServer();
    await new Promise((resolve) => socket.listen(0, '127.0.0.1', resolve));
    const port = socket.address().port;
    await new Promise((resolve) => socket.close(resolve));
    const url = `http://127.0.0.1:${port}`;
    let fakeOllama;
    const chatRequests = [];
    let chatAvailable = true;
    let ollamaHost = process.env.OLLAMA_HOST;
    if (process.env.CODELENS_FAKE_OLLAMA === '1') {
        fakeOllama = http.createServer(async (req, res) => {
            let body = '';
            for await (const chunk of req) body += chunk;
            res.setHeader('Content-Type', 'application/json');
            if (req.url === '/api/tags') {
                res.end(JSON.stringify({ models: [{ name: 'nomic-embed-text:latest' }] }));
            } else if (req.url === '/api/embed') {
                const input = JSON.parse(body).input;
                const texts = Array.isArray(input) ? input : [input];
                res.end(
                    JSON.stringify({
                        model: 'nomic-embed-text',
                        embeddings: texts.map(() => [1, 0, 0, 0]),
                        total_duration: 1,
                        load_duration: 1,
                        prompt_eval_count: 1,
                    }),
                );
            } else if (req.url === '/api/chat') {
                chatRequests.push(JSON.parse(body));
                if (!chatAvailable) {
                    res.writeHead(404);
                    res.end(JSON.stringify({ error: 'explanation model is not installed' }));
                } else {
                    res.end(
                        JSON.stringify({
                            model: 'llama3.2',
                            message: {
                                role: 'assistant',
                                content: 'parse_code_files in parser.py parses source into chunks.',
                            },
                            done: true,
                        }),
                    );
                }
            } else {
                res.writeHead(404);
                res.end(JSON.stringify({ error: 'Unexpected fake Ollama endpoint' }));
            }
        });
        await new Promise((resolve) => fakeOllama.listen(0, '127.0.0.1', resolve));
        ollamaHost = `http://127.0.0.1:${fakeOllama.address().port}`;
    }
    let logs = '';
    const python = path.join(
        root,
        '.venv',
        process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
    );
    const backend = spawn(
        python,
        ['-m', 'uvicorn', 'backend.api:app', '--host', '127.0.0.1', '--port', String(port)],
        {
            cwd: root,
            windowsHide: true,
            env: {
                ...process.env,
                VECTORAI_DB_PATH: path.join(temp, 'db'),
                PYTHONIOENCODING: 'utf-8',
                ...(ollamaHost ? { OLLAMA_HOST: ollamaHost } : {}),
            },
        },
    );
    backend.stdout.on('data', (data) => (logs += data));
    backend.stderr.on('data', (data) => (logs += data));
    backend.on('error', (error) => (logs += error.message));
    try {
        let ready = false;
        for (let attempt = 0; attempt < 100; attempt++) {
            try {
                const response = await fetch(`${url}/status`, {
                    signal: AbortSignal.timeout(3000),
                });
                if (response.ok) {
                    ready = true;
                    break;
                }
            } catch {}
            await new Promise((resolve) => setTimeout(resolve, 200));
        }
        assert.ok(ready, `Backend did not start: ${logs}`);
        const index = await fetch(`${url}/index`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ repo_path: repo }),
            signal: AbortSignal.timeout(120000),
        });
        assert.equal(index.status, 200);
        const events = (await index.text())
            .split('\n\n')
            .filter(Boolean)
            .map((event) => JSON.parse(event.slice(6)));
        assert.ok(!events.some((event) => event.type === 'error'), JSON.stringify(events));
        assert.equal(events.at(-1).total_chunks, 1);
        assert.equal(events.at(-1).watching, true);
        const compiled = path.join(root, 'dist/extension/src/searchPanel.js');
        const module = { exports: {} };
        vm.runInNewContext(fs.readFileSync(compiled, 'utf8'), {
            exports: module.exports,
            require: (name) =>
                name === 'vscode'
                    ? { workspace: { workspaceFolders: [{ uri: { fsPath: repo } }] } }
                    : require(name),
            fetch: (endpoint, options) =>
                fetch(endpoint.replace('http://127.0.0.1:8000', url), options),
            AbortSignal,
            setTimeout,
        });
        const panel = new module.exports.SearchPanelProvider({});
        let message;
        panel._view = { webview: { postMessage: (value) => (message = value) } };
        await panel.handleQuery('where are code files parsed into chunks', false);
        assert.equal(message.type, 'results', JSON.stringify(message));
        assert.ok(message.data.length > 0);
        assert.ok(
            message.data.some((result) => fs.existsSync(path.resolve(repo, result.file_path))),
        );
        assert.equal(message.data[0].file_path, 'parser.py');
        assert.equal(message.explain_text, null);
        if (fakeOllama || process.env.CODELENS_TEST_EXPLAIN === '1') {
            await panel.handleQuery('where are code files parsed into chunks', true);
            assert.equal(message.type, 'results', JSON.stringify(message));
            assert.ok(message.data.length > 0);
            assert.ok(message.explain_text?.trim(), 'Expected an AI explanation');
            assert.ok(!message.explain_text.includes('unavailable'), message.explain_text);
            console.log(`Explanation returned: ${message.explain_text}`);
            if (fakeOllama) {
                assert.equal(chatRequests.length, 1);
                assert.equal(chatRequests[0].stream, false);
                assert.ok(chatRequests[0].messages[1].content.includes('parser.py'));
                chatAvailable = false;
                await panel.handleQuery('parse_code_files', true);
                assert.equal(message.type, 'results', JSON.stringify(message));
                assert.equal(message.data[0].file_path, 'parser.py');
                assert.match(message.explain_text, /Explain mode unavailable/);
            }
        }
        fs.writeFileSync(source, 'def parse_code_files(source):\n    return "watcher_updated"\n');
        let updated = false;
        for (let attempt = 0; attempt < 60; attempt++) {
            await new Promise((resolve) => setTimeout(resolve, 500));
            await panel.handleQuery('parse code files', false);
            if (
                message.type === 'results' &&
                message.data[0]?.chunk_text.includes('watcher_updated')
            ) {
                updated = true;
                break;
            }
        }
        assert.ok(updated, 'Watcher did not update the saved file');
        const secondRepo = path.join(temp, 'second-repo');
        fs.mkdirSync(secondRepo);
        fs.writeFileSync(path.join(secondRepo, 'second.py'), 'def other_workspace(): return 42\n');
        const secondIndex = await fetch(`${url}/index`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ repo_path: secondRepo }),
            signal: AbortSignal.timeout(120000),
        });
        const secondEvents = await secondIndex.text();
        assert.ok(!secondEvents.includes('"type": "error"'), secondEvents);
        await panel.handleQuery('parse_code_files', false);
        assert.equal(message.type, 'results');
        assert.equal(
            message.data[0].file_path,
            'parser.py',
            'Indexing another workspace must preserve the first',
        );
        const secondStatus = await (
            await fetch(`${url}/status?repo_path=${encodeURIComponent(secondRepo)}`)
        ).json();
        assert.equal(secondStatus.indexed_chunks, 1);
        assert.equal(secondStatus.watching, true);
        const mismatch = await fetch(`${url}/query`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query: 'find code', repo_path: root }),
        });
        assert.equal(mismatch.status, 409);
        console.log(
            `${fakeOllama ? 'Fake-Ollama' : 'Real-Ollama'} integration passed: indexing, search, file watcher, independent workspaces`,
        );
    } finally {
        if (backend.exitCode === null) {
            const closed = new Promise((resolve) => backend.once('exit', resolve));
            if (process.platform === 'win32') {
                const killer = spawn('taskkill', ['/pid', String(backend.pid), '/T', '/F'], {
                    windowsHide: true,
                });
                await new Promise((resolve) => killer.once('exit', resolve));
            } else backend.kill();
            await closed;
        }
        if (fakeOllama) await new Promise((resolve) => fakeOllama.close(resolve));
    }
}

checkSearch().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
