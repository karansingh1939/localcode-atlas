const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const root = path.resolve(__dirname, '..');

async function staleSearch() {
    const pending = [];
    const module = { exports: {} };
    vm.runInNewContext(
        fs.readFileSync(path.join(root, 'dist/extension/src/searchPanel.js'), 'utf8'),
        {
            exports: module.exports,
            require: (name) =>
                name === 'vscode'
                    ? { workspace: { workspaceFolders: [{ uri: { fsPath: root } }] } }
                    : require(name),
            fetch: () => new Promise((resolve) => pending.push(resolve)),
            AbortSignal,
        },
    );
    const panel = new module.exports.SearchPanelProvider({});
    const messages = [];
    panel._view = { webview: { postMessage: (message) => messages.push(message) } };
    const response = (label) => ({ ok: true, json: async () => ({ results: [label] }) });
    for (const fails of [false, true]) {
        pending.length = 0;
        messages.length = 0;
        const old = panel.handleQuery('old query', false);
        const latest = panel.handleQuery('latest query', false);
        pending[1](response('latest'));
        await latest;
        pending[0](
            fails
                ? { ok: false, status: 500, json: async () => ({ detail: 'old error' }) }
                : response('old'),
        );
        await old;
        assert.equal(messages.at(-1).data[0], 'latest');
        assert.equal(messages.length, 1);
    }
    console.log('Latest query results are preserved');
}

async function sharedBackend() {
    const spawns = [];
    let running = false;
    function loadWindow() {
        const module = { exports: {} };
        const child = new EventEmitter();
        child.pid = 4321;
        child.kill = () => {
            running = false;
        };
        child.unref = () => {};
        const vscode = {
            window: {
                createStatusBarItem: () => ({ show() {} }),
                registerWebviewViewProvider: () => ({}),
                showErrorMessage() {},
            },
            commands: { registerCommand: () => ({}) },
            StatusBarAlignment: { Left: 1 },
        };
        const mocks = {
            vscode,
            './searchPanel': { SearchPanelProvider: class {} },
            child_process: {
                execFileSync() {},
                spawn(command, args) {
                    spawns.push({ command, args });
                    running = command !== 'taskkill';
                    return child;
                },
            },
        };
        vm.runInNewContext(
            fs.readFileSync(path.join(root, 'dist/extension/src/extension.js'), 'utf8'),
            {
                exports: module.exports,
                require: (name) => mocks[name] || require(name),
                process,
                console,
                AbortSignal,
                fetch: async () => {
                    if (!running) throw new Error('offline');
                    return {
                        ok: true,
                        json: async () => ({ indexed_chunks: 1, embed_model: 'test' }),
                    };
                },
                setTimeout() {},
                clearTimeout() {},
                clearInterval() {},
            },
        );
        return module.exports;
    }
    const a = loadWindow();
    const b = loadWindow();
    const context = () => ({ extensionPath: root, extensionUri: {}, subscriptions: [] });
    await a.activate(context());
    await new Promise(setImmediate);
    await b.activate(context());
    await new Promise(setImmediate);
    assert.equal(spawns.length, 1);
    a.deactivate();
    assert.equal(running, true);
    assert.equal(spawns.length, 1);
    console.log('Shared backend survives owner window shutdown');
    b.deactivate();
}

(async () => {
    await staleSearch();
    await sharedBackend();
})().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
