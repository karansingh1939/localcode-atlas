const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const root = path.resolve(__dirname, '..');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'codelens-install-'));
const python = path.join(
    root,
    '.venv',
    process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
);
function run(command, args, options = {}) {
    const result = spawnSync(command, args, { stdio: 'inherit', windowsHide: true, ...options });
    if (result.error) throw result.error;
    assert.equal(result.status, 0, `Command failed: ${command}`);
}

try {
    run(python, [
        '-c',
        'import sys,zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])',
        path.join(root, 'localcode-atlas-0.2.0.vsix'),
        temp,
    ]);
    const installed = path.join(temp, 'extension');
    run('powershell', [
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        path.join(root, 'tests/setup_package.ps1'),
        '-SetupPath',
        path.join(installed, 'setup.ps1'),
    ]);
    for (const file of [
        'backend/api.py',
        'package.json',
        'package-lock.json',
        'setup.ps1',
        'requirements-windows.lock',
        'extension/media/panel.html',
        'LICENSE.txt',
    ]) {
        assert.ok(fs.existsSync(path.join(installed, file)), `Missing packaged file: ${file}`);
    }
    const manifest = JSON.parse(fs.readFileSync(path.join(installed, 'package.json'), 'utf8'));
    assert.equal(manifest.name, 'localcode-atlas');
    assert.equal(manifest.version, '0.2.0');
    assert.equal(manifest.license, 'MIT');
    assert.equal(manifest.repository.url, 'https://github.com/karansingh-1939/localcode-atlas.git');
    assert.match(fs.readFileSync(path.join(installed, 'LICENSE.txt'), 'utf8'), /Nikhil Mourya/);
    run(python, ['-m', 'venv', path.join(installed, '.venv')]);
    const freshPython = path.join(
        installed,
        '.venv',
        process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
    );
    run(freshPython, ['-m', 'pip', 'install', '-r', 'requirements-windows.lock'], {
        cwd: installed,
    });
    run(freshPython, ['-m', 'pip', 'install', '--no-deps', '-e', '.'], { cwd: installed });
    run(freshPython, ['-m', 'pip', 'check'], { cwd: installed });
    run(process.execPath, [path.join(root, 'tests/extension_search.cjs')], {
        cwd: installed,
        env: { ...process.env, CODELENS_TEST_ROOT: installed, CODELENS_FAKE_OLLAMA: '1' },
    });
    console.log(`Fresh VSIX installation passed in ${installed}`);
} catch (error) {
    console.error(error);
    process.exitCode = 1;
}
