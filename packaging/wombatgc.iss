; Inno Setup script for the Windows installer.  Built by .github/workflows/build.yml:
;   iscc /DAppVersion=2.0 packaging\wombatgc.iss
; Installs for the current user only (no administrator rights needed).

#ifndef AppVersion
  #define AppVersion "0.0"
#endif
#define AppName "WombatGC"

[Setup]
AppId={{6F1C2E8A-4B7D-4C55-9E3A-57A1D2C0B9E1}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=University of Plymouth
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\installers
OutputBaseFilename={#AppName}-{#AppVersion}-Windows-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppName}.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppName}.exe"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppName}.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppName}.exe"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent
