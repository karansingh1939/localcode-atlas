import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import { spawn, ChildProcess, execFileSync } from 'child_process';
import { SearchPanelProvider } from './searchPanel';

let backendProcess: ChildProcess | undefined;
let statusBarItem: vscode.StatusBarItem;
let pollInterval: NodeJS.Timeout;
let startupTimeout: NodeJS.Timeout;

const PORT = 8000;
const API_URL = `http://127.0.0.1:${PORT}`;

/** Find a Python interpreter that can import the backend (project .venv first). */
function detectPython(root: string): string {
    const candidates = [
        path.join(
            root,
            '.venv',
            process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
        ),
        'python',
        'python3',
        'py',
        '/usr/local/bin/python3.11',
        '/usr/local/bin/python3.12',
        '/usr/local/bin/python3',
        '/usr/bin/python3',
    ];
    for (const p of candidates) {
        try {
            execFileSync(p, ['-c', 'import uvicorn, backend.api'], {
                cwd: root,
                stdio: 'ignore',
                windowsHide: true,
                timeout: 15000,
            });
            return p;
        } catch {
            /* try next */
        }
    }
    throw new Error(
        'LocalCode Atlas Python dependencies are missing. Set up the project .venv first.',
    );
}

export async function activate(context: vscode.ExtensionContext) {
    // Sidebar webview
    const provider = new SearchPanelProvider(context.extensionUri);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider('codelens.sidebar', provider),
    );

    // Status bar
    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    statusBarItem.text = '$(sync~spin) LocalCode Atlas starting...';
    statusBarItem.show();
    context.subscriptions.push(statusBarItem);

    startBackendServer(context).catch((error: Error) => {
        statusBarItem.text = '$(database) LocalCode Atlas: Offline';
        vscode.window.showErrorMessage(error.message);
    });

    // Commands
    context.subscriptions.push(
        vscode.commands.registerCommand('codelens.search', () => {
            vscode.commands.executeCommand('codelens.sidebar.focus');
        }),
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('codelens.reindex', () => {
            provider.triggerReindex();
        }),
    );

    context.subscriptions.push(
        vscode.commands.registerCommand('codelens.showStatus', () => {
            vscode.commands.executeCommand('codelens.sidebar.focus');
        }),
    );

    // Warn once if Ollama is unavailable, then keep the status bar current.
    startupTimeout = setTimeout(async () => {
        try {
            const response = await fetch(`${API_URL}/health`, {
                signal: AbortSignal.timeout(2000),
            });
            const health = (await response.json()) as { ollama: boolean };

            if (health.ollama === false) {
                const actionBtn = 'Install Ollama';
                const installRes = await vscode.window.showWarningMessage(
                    'LocalCode Atlas: Ollama was not found or is not running. It is required to index and search code.',
                    actionBtn,
                );
                if (installRes === actionBtn) {
                    vscode.env.openExternal(vscode.Uri.parse('https://ollama.com/download'));
                }
            }
        } catch {
            console.error('[LocalCode Atlas] Backend health check failed.');
        }

        startPolling();
    }, 4500); // Give uvicorn time to start.
}

async function startBackendServer(context: vscode.ExtensionContext) {
    try {
        const health = await fetch(`${API_URL}/status`, { signal: AbortSignal.timeout(2000) });
        const status = health.ok
            ? ((await health.json()) as { indexed_chunks?: number; embed_model?: string })
            : undefined;
        if (typeof status?.indexed_chunks === 'number' && typeof status.embed_model === 'string') {
            console.log(`[LocalCode Atlas]: Backend is already active on port ${PORT}.`);
            statusBarItem.text = '$(database) LocalCode Atlas: Ready';
            return;
        }
    } catch {
        // Backend not active yet, proceed to spawn
    }

    const extensionRoot = context.extensionPath || path.resolve(__dirname, '../../..');
    const cwd = extensionRoot;
    if (!fs.existsSync(path.join(cwd, 'backend', 'api.py'))) {
        throw new Error('LocalCode Atlas backend is missing from the extension installation.');
    }

    backendProcess = spawn(
        detectPython(cwd),
        ['-m', 'uvicorn', 'backend.api:app', '--host', '127.0.0.1', '--port', PORT.toString()],
        {
            cwd,
            // Several VS Code windows share this service; none owns its lifetime.
            detached: true,
            stdio: 'ignore',
            windowsHide: true,
            env: {
                ...process.env,
                PYTHONPATH:
                    cwd + (process.env.PYTHONPATH ? path.delimiter + process.env.PYTHONPATH : ''),
                PYTHONIOENCODING: 'utf-8',
            },
        },
    );
    backendProcess.unref();

    backendProcess.on('error', (error) => {
        statusBarItem.text = '$(database) LocalCode Atlas: Offline';
        vscode.window.showErrorMessage(`LocalCode Atlas could not start Python: ${error.message}`);
    });
    backendProcess.on('exit', (code) => {
        console.warn(`[LocalCode Atlas] Backend exited with code ${code}`);
        statusBarItem.text = '$(database) LocalCode Atlas: Offline';
    });
}

function startPolling() {
    pollStatus();
    pollInterval = setInterval(pollStatus, 30000); // updates every 30s
}

async function pollStatus() {
    try {
        const repo = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
        const res = await fetch(
            `${API_URL}/status${repo ? '?repo_path=' + encodeURIComponent(repo) : ''}`,
            { signal: AbortSignal.timeout(2000) },
        );
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = (await res.json()) as { indexed_chunks: number };
        const chunks = data.indexed_chunks || 0;
        statusBarItem.text = `$(database) LocalCode Atlas: ${chunks} chunks`;
    } catch {
        statusBarItem.text = '$(database) LocalCode Atlas: Offline';
    }
}

export function deactivate() {
    if (startupTimeout) clearTimeout(startupTimeout);
    if (pollInterval) clearInterval(pollInterval);
}
