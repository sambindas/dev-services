#define AppVersion "0.2.1"
[Setup]
AppId=DevServices.LocalDashboard
AppName=Dev Services
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\DevServices
DefaultGroupName=Dev Services
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=DevServices-{#AppVersion}-Windows-Setup
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\DevServices.exe

[Files]
Source: "..\dist\DevServices\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Dev Services"; Filename: "{app}\DevServices.exe"
Name: "{userdesktop}\Dev Services"; Filename: "{app}\DevServices.exe"; Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\DevServices.exe"; Description: "Open Dev Services"; Flags: nowait postinstall skipifsilent
