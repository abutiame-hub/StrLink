; StrLink installer script - built with Inno Setup (https://jrsoftware.org/isinfo.php).
;
; This turns the portable StrLink.exe into a real Windows install: Start Menu
; shortcut, optional Desktop shortcut, a proper entry in Settings > Apps /
; Add-Remove Programs with a working Uninstall, and an install location under
; Program Files instead of a folder the user has to manage by hand.
;
; It intentionally does NOT bundle Skills_Library/Templates/Personal_Data or
; any snapshot/recovery data - those are the user's data, not the program,
; and StrLink already knows how to create or reuse a data folder on first run
; (see default_backup_dir() in backend.py). Installing/uninstalling the app
; never touches that data.
;
; To build: open this file in Inno Setup (Ctrl+F9), or from a command line:
;   ISCC.exe StrLink.iss
; (ISCC.exe lives wherever Inno Setup 6 was installed - Program Files for a
; system-wide install, or %LocalAppData%\Programs\Inno Setup 6 for a
; per-user install.)
; The finished installer is written to app_src\installer\Output\.

#define MyAppName "StrLink"
#define MyAppVersion "2.0.0"
#define MyAppPublisher "TYAMEELKHOLY"
#define MyAppURL "https://github.com/"
#define MyAppExeName "StrLink.exe"

[Setup]
AppId={{B6E4C6C4-6F1D-4B6A-9B2E-6C6F6C6F6F01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Per-user install by default so no admin prompt is required; the installer
; still offers "install for all users" via PrivilegesRequiredOverridesAllowed.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=Output
OutputBaseFilename=StrLink_Setup_{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=..\assets\StrLink.ico
ArchitecturesInstallIn64BitMode=x64compatible
DisableWelcomePage=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The whole one-file build; StrLink's own UI/backend already live inside it
; (PyInstaller onefile), so this is the only binary that needs shipping.
Source: "..\..\StrLink.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; StrLink writes its own log/preferences under %LOCALAPPDATA%\StrLink at
; runtime (see get_log_dir()/settings_path() in the source) - that is user
; data (logs, the saved backup-folder preference), not program files, so
; uninstalling the app deliberately leaves it in place rather than deleting
; it silently. Remove it by hand if you want a fully clean slate.
