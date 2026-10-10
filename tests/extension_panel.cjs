const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const http = require('node:http');

async function run() {
    const html = fs.readFileSync(path.resolve(__dirname, '../extension/media/panel.html'), 'utf8');
    const parser = html.match(/const syntaxParser = ([\s\S]*?);\s*function spawnShimmerLoaders/)[1];
    const highlight = vm.runInNewContext(`(${parser}).highlight`);
    assert.equal(highlight('"class"'), '<span class="hljs-string">"class"</span>');
    assert.ok(!highlight('<script>alert("x")</script>').includes('<script>'));

    let status = 409;
    const server = http.createServer((req, res) => {
        req.resume();
        if (status === 409) {
            res.writeHead(409, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ detail: 'Indexing is already in progress.' }));
        } else {
            res.writeHead(200, { 'Content-Type': 'text/event-stream' });
            const bytes = Buffer.from(
                'data: {"type":"progress","file":"café.py","chunks":1,"processed_files":1,"total_files":1}\n\n',
            );
            const split = bytes.indexOf(Buffer.from('é')) + 1;
            res.write(bytes.subarray(0, split));
            setImmediate(() => {
                res.write(bytes.subarray(split));
                res.end(
                    'data: {"type":"complete","total_chunks":1,"watching":true,"failed_files":0}\n\n',
                );
            });
        }
    });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    try {
        const module = { exports: {} };
        vm.runInNewContext(
            fs.readFileSync(
                path.resolve(__dirname, '../dist/extension/src/searchPanel.js'),
                'utf8',
            ),
            {
                exports: module.exports,
                require: (name) => {
                    if (name === 'vscode')
                        return {
                            workspace: {
                                workspaceFolders: [{ uri: { fsPath: __dirname } }],
                                getConfiguration: () => ({ get: (key, fallback) => fallback }),
                            },
                        };
                    if (name === 'http')
                        return {
                            request: (url, options, callback) =>
                                http.request(
                                    `http://127.0.0.1:${server.address().port}/index`,
                                    options,
                                    callback,
                                ),
                        };
                    return require(name);
                },
                Buffer,
                setTimeout,
            },
        );
        const panel = new module.exports.SearchPanelProvider({});
        async function index() {
            const messages = [];
            await new Promise((resolve, reject) => {
                const timeout = setTimeout(() => reject(new Error('No indexing response')), 3000);
                panel._view = {
                    webview: {
                        postMessage: (message) => {
                            messages.push(message);
                            if (message.type === 'error' || message.type === 'status') {
                                clearTimeout(timeout);
                                resolve();
                            }
                        },
                    },
                };
                panel.triggerReindex();
            });
            return messages;
        }
        const errors = await index();
        assert.equal(errors[0].message, 'Indexing is already in progress.');
        status = 200;
        const messages = await index();
        assert.equal(messages[0].file, 'café.py');
        assert.equal(messages.at(-1).chunks, 1);
        console.log('Panel checks passed: highlighting, HTTP errors, Unicode SSE');
    } finally {
        await new Promise((resolve) => server.close(resolve));
    }
}

run().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
