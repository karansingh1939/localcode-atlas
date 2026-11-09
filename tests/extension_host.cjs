// Run in VS Code with --extensionTestsPath=<absolute path to this file>.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vscode = require('vscode');

exports.run = async () => {
    const extension = vscode.extensions.getExtension('karansingh1939.localcode-atlas');
    assert.ok(extension, 'LocalCode Atlas extension is loaded');
    await extension.activate();
    const commands = await vscode.commands.getCommands();
    assert.ok(commands.includes('codelens.search'));
    await vscode.commands.executeCommand('codelens.search');
    const { SearchPanelProvider } = require('../dist/extension/src/searchPanel');
    const provider = new SearchPanelProvider(extension.extensionUri);
    let message;
    provider._view = {
        webview: {
            postMessage: (value) => {
                message = value;
            },
        },
    };
    await provider.handleQuery('where are code files parsed into chunks', false);
    assert.equal(message.type, 'results', JSON.stringify(message));
    assert.ok(message.data.length > 0, 'Search returns indexed code');
    const result = message.data.find((r) =>
        fs.existsSync(path.join(vscode.workspace.workspaceFolders[0].uri.fsPath, r.file_path)),
    );
    assert.ok(result, 'Search result points to an existing file');
    await provider.handleJumpTo(result.file_path, result.line_start);
    assert.equal(
        vscode.window.activeTextEditor.document.uri.fsPath,
        path.join(vscode.workspace.workspaceFolders[0].uri.fsPath, result.file_path),
    );
    assert.equal(vscode.window.activeTextEditor.selection.active.line, result.line_start - 1);
    console.log(
        'LOCALCODE_ATLAS_HOST_SMOKE_PASSED: activation, sidebar command, real search, jump to file',
    );
};
