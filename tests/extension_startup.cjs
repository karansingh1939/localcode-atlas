const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');

async function checkStartup() {
    const root = path.resolve(__dirname, '..');
    let spawned;
    const child = new EventEmitter();
    child.pid = 1234;
    child.kill = () => {};
    let unreferenced = false;
    child.unref = () => {
        unreferenced = true;
    };
    const vscode = {
        workspace: { workspaceFolders: [{ uri: { fsPath: root } }] },
        window: {
            createStatusBarItem: () => ({ show() {} }),
            registerWebviewViewProvider: () => ({}),
            showErrorMessage: () => {},
        },
        commands: { registerCommand: () => ({}) },
        StatusBarAlignment: { Left: 1 },
    };
    const extension = { exports: {} };
    const mocks = {
        vscode,
        './searchPanel': { SearchPanelProvider: class {} },
        child_process: {
            execSync() {},
            execFileSync() {},
            spawn: (python, args, options) => {
                spawned = { python, args, options };
                return child;
            },
        },
    };
    vm.runInNewContext(
        fs.readFileSync(path.join(root, 'dist/extension/src/extension.js'), 'utf8'),
        {
            exports: extension.exports,
            require: (name) => mocks[name] || require(name),
            process,
            console,
            fetch: async () => {
                throw new Error('Backend stopped');
            },
            __dirname: path.join(root, 'dist/extension/src'),
            setTimeout() {},
            clearInterval() {},
        },
    );
    await extension.exports.activate({ extensionPath: root, extensionUri: {}, subscriptions: [] });
    await new Promise(setImmediate);
    assert.equal(
        spawned.python,
        path.join(
            root,
            '.venv',
            process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
        ),
    );
    assert.equal(spawned.options.cwd, root);
    assert.equal(spawned.options.windowsHide, true);
    assert.equal(spawned.options.detached, true);
    assert.equal(spawned.options.stdio, 'ignore');
    assert.equal(unreferenced, true);
    assert.ok(child.listenerCount('error'), 'Spawn failures must be handled');
    child.emit('error', new Error('Python unavailable'));
    extension.exports.deactivate();
    assert.notEqual(spawned.python, 'taskkill', 'Keep the shared backend alive');
    console.log('Extension startup check passed');
}
checkStartup().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
