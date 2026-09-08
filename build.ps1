$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$buildPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $buildPython)) { throw 'Install requirements in .venv before building.' }
& $buildPython -m pytest tests -q -p no:cacheprovider
if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
& $buildPython -m PyInstaller --noconfirm --clean --windowed --onedir --name SmartOps --add-data 'smartops_desktop/recorder.js;smartops_desktop' --hidden-import win32service --hidden-import win32gui --hidden-import win32con --hidden-import win32clipboard --hidden-import uiautomation --collect-all playwright --copy-metadata PySide6 --copy-metadata PySide6_Essentials --copy-metadata shiboken6 --copy-metadata openpyxl --copy-metadata PyYAML --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets --exclude-module numpy --exclude-module pandas main.py
if ($LASTEXITCODE -ne 0) { throw 'Packaging failed.' }
Copy-Item -LiteralPath 'README.md' -Destination 'dist/SmartOps/START-HERE.md'
Copy-Item -LiteralPath 'THIRD-PARTY-NOTICES.md' -Destination 'dist/SmartOps/THIRD-PARTY-NOTICES.md'
$buildSource = New-Item -ItemType Directory -Path 'dist/SmartOps/source' -Force
Copy-Item -LiteralPath 'main.py','requirements.txt','build.ps1','README.md','THIRD-PARTY-NOTICES.md' -Destination $buildSource.FullName
foreach ($buildFolder in @('smartops_desktop','tests')) {
    $buildDestination = New-Item -ItemType Directory -Path (Join-Path $buildSource.FullName $buildFolder) -Force
    Get-ChildItem -LiteralPath $buildFolder -File | Copy-Item -Destination $buildDestination.FullName
}
$env:SMARTOPS_SELFTEST_DIR = Join-Path $PSScriptRoot 'verification\packaged'
$buildProcess = Start-Process -FilePath (Join-Path $PSScriptRoot 'dist\SmartOps\SmartOps.exe') -ArgumentList '--self-test' -PassThru -WindowStyle Hidden
if (-not $buildProcess.WaitForExit(60000)) { Stop-Process -Id $buildProcess.Id; throw 'Packaged self-test timed out.' }
if ($buildProcess.ExitCode -ne 0) { throw 'Packaged self-test failed.' }
$buildResult = Get-Content -LiteralPath 'verification/packaged/selftest.json' -Raw | ConvertFrom-Json
if (-not $buildResult.passed) { throw 'Packaged demo did not pass.' }
Compress-Archive -LiteralPath 'dist/SmartOps' -DestinationPath 'SmartOps-Desktop-v0.1-Windows.zip' -Force
Get-FileHash -LiteralPath 'SmartOps-Desktop-v0.1-Windows.zip' -Algorithm SHA256
