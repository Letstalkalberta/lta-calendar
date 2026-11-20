# Build LTA Watcher on a Windows PC (the GitHub workflow does the same thing automatically).
# Needs: Python 3.12 (python.org) and Inno Setup 6 (jrsoftware.org). Run from the repository root:
#   powershell -ExecutionPolicy Bypass -File desktop\build.ps1
$ErrorActionPreference = "Stop"
$version = (Select-String -Path desktop\lta_watcher\__init__.py -Pattern 'VERSION = "(.+)"').Matches[0].Groups[1].Value
python -m pip install --upgrade pip
python -m pip install -r desktop\requirements.txt pyinstaller
python -m PyInstaller --noconfirm --clean --windowed --name "LTA Watcher" `
  --icon desktop\assets\lta.ico `
  --paths desktop `
  --add-data "desktop\lta_watcher\ui;ui" `
  --add-data "watcher\watch.py;watcher" `
  --hidden-import pystray._win32 `
  desktop\run_lta_watcher.py
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" /DAppVersion=$version desktop\installer.iss
Write-Host "Done: dist\LTA-Watcher-Setup-$version.exe"
