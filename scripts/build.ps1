$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDir
$pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Run setup.cmd first.' }
& $pythonExe -m pip install --disable-pip-version-check -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw 'Build dependencies could not be installed.' }
& $pythonExe -m pytest -q
if ($LASTEXITCODE -ne 0) { throw 'Tests failed; no package was built.' }
$basePythonDir = & $pythonExe -c 'import sys; print(sys.base_prefix)'
$originalBuildPath = $env:PATH
try {
    # Exclude unrelated native tools: their ICU DLLs can shadow the Windows ICU used by Qt.
    $env:PATH = @((Split-Path -Parent $pythonExe), $basePythonDir, (Join-Path $basePythonDir 'DLLs'), (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot) -join ';'
    & $pythonExe -m PyInstaller --noconfirm --clean --windowed --onedir --name Shiguang --icon assets/app.ico --add-data 'assets/app.ico;assets' --copy-metadata PySide6-Essentials --copy-metadata shiboken6 --copy-metadata instaloader --copy-metadata Pillow --copy-metadata requests main.py
    if ($LASTEXITCODE -ne 0) { throw 'Packaging failed.' }
} finally {
    $env:PATH = $originalBuildPath
}
Copy-Item -LiteralPath (Join-Path $projectDir 'README.md') -Destination (Join-Path $projectDir 'dist\Shiguang\README.md')
& (Join-Path $PSScriptRoot 'check_package.ps1')
Write-Host 'Ready: dist\Shiguang\Shiguang.exe. Keep the complete Shiguang folder together.'
