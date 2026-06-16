; Inno Setup script for EchosMonitor (M7-C2).
; Wraps the PyInstaller one-dir bundle (dist\echosmonitor\) into a Windows
; installer. Build with:
;   iscc /DMyAppVersion=<version> packaging\windows\echosmonitor.iss
; Output: dist\EchosMonitor-<version>-windows-setup.exe
; Paths are relative to this .iss file (packaging\windows\).

#define MyAppName "EchosMonitor"
#define MyAppExeName "echosmonitor.exe"
#define MyAppPublisher "Echos"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
; A stable AppId keeps upgrades/uninstalls associated across versions.
AppId={{6F3E2A10-9C4B-4E7D-8A21-2B5C9D0E1F34}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=..\..\dist
OutputBaseFilename=EchosMonitor-{#MyAppVersion}-windows-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\dist\echosmonitor\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
