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
$packagedDocs = Join-Path $projectDir 'dist\Shiguang\docs'
New-Item -ItemType Directory -Force -Path $packagedDocs | Out-Null
Copy-Item -LiteralPath (Join-Path $projectDir 'docs\version-and-sharing.md'), (Join-Path $projectDir 'docs\v2-design.md') -Destination $packagedDocs
& (Join-Path $PSScriptRoot 'check_package.ps1')
$version = & $pythonExe -c 'from ins_saver import __version__; print(__version__)'
$releaseDir = Join-Path $projectDir 'releases'
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
$releaseArchive = Join-Path $releaseDir "Shiguang-v$version-windows-x64.zip"
$zipCode = @'
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import sys
source, target = Path(sys.argv[1]), Path(sys.argv[2])
temporary = target.with_suffix('.zip.part')
try:
    with ZipFile(temporary, 'w', ZIP_DEFLATED, compresslevel=6) as archive:
        for item in sorted(source.rglob('*')):
            if item.is_file():
                archive.write(item, item.relative_to(source.parent).as_posix())
    temporary.replace(target)
finally:
    temporary.unlink(missing_ok=True)
'@
$zipCode | & $pythonExe - (Join-Path $projectDir 'dist\Shiguang') $releaseArchive
if ($LASTEXITCODE -ne 0) { throw 'Release archive creation failed.' }
Write-Host 'Ready: dist\Shiguang\Shiguang.exe. Keep the complete Shiguang folder together.'
Write-Host "Portable ZIP: $releaseArchive"
