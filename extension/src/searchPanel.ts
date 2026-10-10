import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import * as http from 'http';
import { randomBytes } from 'crypto';
import { StringDecoder } from 'string_decoder';

export class SearchPanelProvider implements vscode.WebviewViewProvider {
    private _view?: vscode.WebviewView;

    constructor(private readonly _extensionUri: vscode.Uri) {}

    public resolveWebviewView(
        webviewView: vscode.WebviewView,
        context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken,
    ) {
        this._view = webviewView;
        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [this._extensionUri],
        };

        webviewView.webview.html = this.getHtmlForWebview();

        // 1. Process Messages dispatched from panel.html WebView runtime
        webviewView.webview.onDidReceiveMessage(async (data) => {
            switch (data?.type) {
                case 'query':
                    if (typeof data.text === 'string')
                        await this.handleQuery(data.text, Boolean(data.explain));
                    break;
                case 'jumpTo':
                    if (typeof data.file === 'string' && Number.isInteger(data.line))
                        await this.handleJumpTo(data.file, data.line);
                    break;
                case 'reindex':
                    this.handleReindex();
                    break;
            }
        });

        // 2. Fetch and push initial status to the webview immediately
        this.pushStatus();
    }

    public async pushStatus() {
        for (let attempt = 0; attempt < 2; attempt++) {
            try {
                const repo = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
                const response = await fetch(
                    `http://127.0.0.1:8000/status${repo ? '?repo_path=' + encodeURIComponent(repo) : ''}`,
                    {
                        signal: AbortSignal.timeout(2000),
                    },
                );
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const status = (await response.json()) as {
                    indexed_chunks: number;
                    watching: boolean;
                };
                this._view?.webview.postMessage({
                    type: 'status',
                    chunks: status.indexed_chunks,
                    watching: status.watching,
                });
                return;
            } catch {
                if (attempt === 0) await new Promise((resolve) => setTimeout(resolve, 2500));
            }
        }
    }

    public triggerReindex() {
        this.handleReindex();
    }

    private async handleQuery(text: string, explain: boolean) {
        try {
            const response = await fetch('http://127.0.0.1:8000/query', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    query: text,
                    top_k: 8,
                    explain,
                    repo_path: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath,
                }),
                signal: AbortSignal.timeout(30000),
            });
            if (!response.ok) {
                const error = (await response.json()) as { detail?: string };
                throw new Error(error.detail || `Search failed: HTTP ${response.status}`);
            }
            const result = (await response.json()) as {
                results: unknown[];
                explain_text: string | null;
                query_ms: number;
                total_indexed: number;
            };
            this._view?.webview.postMessage({
                type: 'results',
                data: result.results,
                explain_text: result.explain_text,
                query_ms: result.query_ms,
                total_indexed: result.total_indexed,
            });
        } catch (error: any) {
            this._view?.webview.postMessage({ type: 'error', message: error.message });
        }
    }

    private async handleReindex() {
        const workspaceDir = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
        if (!workspaceDir) {
            this._view?.webview.postMessage({
                type: 'error',
                message: 'No valid workspace currently open.',
            });
            return;
        }

        const excludePatterns = vscode.workspace
            .getConfiguration('codelens')
            .get<string[]>('excludePatterns', []);
        const payload = JSON.stringify({
            repo_path: workspaceDir,
            force_reindex: false,
            exclude_patterns: excludePatterns,
        });

        let buffer = '';
        const decoder = new StringDecoder('utf8');
        let completed = false;
        const reportError = (message: string) =>
            this._view?.webview.postMessage({ type: 'error', message });
        const req = http.request(
            'http://127.0.0.1:8000/index',
            {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Content-Length': Buffer.byteLength(payload),
                },
            },
            (res) => {
                if (res.statusCode !== 200) {
                    let body = '';
                    res.on('data', (chunk) => (body += chunk.toString()));
                    res.on('end', () => {
                        try {
                            reportError(
                                JSON.parse(body).detail ||
                                    `Indexing failed: HTTP ${res.statusCode}`,
                            );
                        } catch {
                            reportError(`Indexing failed: HTTP ${res.statusCode}`);
                        }
                    });
                    return;
                }
                res.on('error', (error) => reportError(error.message));
                res.on('end', () => {
                    if (!completed)
                        reportError(
                            'Indexing connection ended before completion. Re-index to retry.',
                        );
                });
                res.on('data', (bufferChunk) => {
                    buffer += decoder.write(bufferChunk);
                    const events = buffer.split('\n\n');
                    buffer = events.pop() || '';

                    for (const ev of events) {
                        if (ev.startsWith('data: ')) {
                            try {
                                const parsed = JSON.parse(ev.substring(6));
                                if (parsed.type === 'progress') {
                                    // Maps exactly onto the Webview message protocol specified
                                    this._view?.webview.postMessage({
                                        type: 'indexProgress',
                                        file: parsed.file,
                                        pct:
                                            ((parsed.processed_files ?? parsed.chunks) /
                                                Math.max(1, parsed.total_files)) *
                                            100,
                                        message: `${parsed.chunks} chunks found`,
                                    });
                                } else if (parsed.type === 'complete') {
                                    completed = true;
                                    this._view?.webview.postMessage({
                                        type: 'status',
                                        chunks: parsed.total_chunks,
                                        watching: parsed.watching,
                                    });
                                    if (parsed.failed_files)
                                        reportError(
                                            `Indexing finished with ${parsed.failed_files} failed files. Check Ollama and re-index to retry.`,
                                        );
                                } else if (parsed.type === 'error') {
                                    this._view?.webview.postMessage({
                                        type: 'error',
                                        message: parsed.message,
                                    });
                                }
                            } catch (e) {
                                // Ignored partial streaming fragments
                            }
                        }
                    }
                });
            },
        );

        req.setTimeout(120000, () =>
            req.destroy(new Error('Indexing timed out. Check Ollama and retry.')),
        );

        req.on('error', (e) => {
            this._view?.webview.postMessage({
                type: 'error',
                message: `Indexing failed: ${e.message}`,
            });
        });

        req.write(payload);
        req.end();
    }

    private async handleJumpTo(file: string, line: number) {
        const workspace = vscode.workspace.workspaceFolders?.[0].uri.fsPath;
        if (!workspace) return;

        const absolutePath = path.resolve(workspace, file);
        const relativePath = path.relative(workspace, absolutePath);
        if (
            relativePath === '..' ||
            relativePath.startsWith(`..${path.sep}`) ||
            path.isAbsolute(relativePath)
        ) {
            vscode.window.showErrorMessage('Result is outside the workspace.');
            return;
        }
        try {
            const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(absolutePath));
            const editor = await vscode.window.showTextDocument(doc);

            // Adjust to VS Code internal 0-indexed engine structure safely mapping bounds
            const vsLine = Math.min(doc.lineCount - 1, Math.max(0, line - 1));
            const range = new vscode.Range(vsLine, 0, vsLine, 0);

            editor.selection = new vscode.Selection(range.start, range.end);
            editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
        } catch (e: any) {
            vscode.window.showErrorMessage(`Cannot open file: ${e.message}`);
        }
    }

    private getHtmlForWebview() {
        const candidates = [
            vscode.Uri.joinPath(this._extensionUri, 'extension', 'media', 'panel.html'),
            vscode.Uri.joinPath(this._extensionUri, 'media', 'panel.html'),
        ];
        for (const candidate of candidates) {
            if (fs.existsSync(candidate.fsPath)) {
                const nonce = randomBytes(16).toString('base64');
                return fs
                    .readFileSync(candidate.fsPath, 'utf-8')
                    .replace(
                        '<head>',
                        `<head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; style-src-attr 'unsafe-inline'; script-src 'nonce-${nonce}';">`,
                    )
                    .replace('<style>', `<style nonce="${nonce}">`)
                    .replace('<script>', `<script nonce="${nonce}">`);
            }
        }
        throw new Error(`LocalCode Atlas panel.html not found in extension bundle.`);
    }
}
