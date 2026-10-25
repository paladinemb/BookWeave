[Setup]
AppId={{56191C5C-F90A-48F7-BC63-93514258EF9C}
AppName=BookWeave
AppVersion={#AppVersion}
AppPublisher=BookWeave
DefaultDirName={localappdata}\Programs\BookWeave
DefaultGroupName=BookWeave
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\release-output
OutputBaseFilename=BookWeave-{#AppVersion}-windows-x64-setup
SetupIconFile=..\build\BookWeave.ico
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\BookWeave.exe
CloseApplications=yes
[Files]
Source: "..\dist\BookWeave\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\BookWeave"; Filename: "{app}\BookWeave.exe"
Name: "{autodesktop}\BookWeave"; Filename: "{app}\BookWeave.exe"
