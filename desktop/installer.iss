; Inno Setup script for LTA Watcher. Built by .github/workflows/build-windows.yml
; (or locally: build.ps1). Per-user install: no administrator rights needed.
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{7C1E2B0A-4D6F-4E8B-9A3C-1F2D3E4A5B6C}
AppName=LTA Watcher
AppVersion={#AppVersion}
AppPublisher=Let's Talk Alberta Society
AppPublisherURL=https://www.letstalkalberta.com
DefaultDirName={localappdata}\Programs\LTA Watcher
DefaultGroupName=LTA Watcher
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=LTA-Watcher-Setup-{#AppVersion}
SetupIconFile=assets\lta.ico
UninstallDisplayIcon={app}\LTA Watcher.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "autostart"; Description: "Start LTA Watcher in the background when I sign in to Windows (recommended)"; Flags: checkedonce
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist\LTA Watcher\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\LTA Watcher"; Filename: "{app}\LTA Watcher.exe"
Name: "{userdesktop}\LTA Watcher"; Filename: "{app}\LTA Watcher.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "LTA Watcher"; ValueData: """{app}\LTA Watcher.exe"" --background"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
; Runs after a normal install (as a checkbox) and after a silent auto-update.
Filename: "{app}\LTA Watcher.exe"; Description: "Open LTA Watcher now"; Flags: nowait postinstall

[UninstallRun]
Filename: "taskkill"; Parameters: "/F /IM ""LTA Watcher.exe"""; Flags: runhidden; RunOnceId: "KillApp"
