[Setup]
AppName=Jarvis
AppVersion=1.0.0
DefaultDirName={autopf}\Jarvis
DefaultGroupName=Jarvis
OutputDir=installer
OutputBaseFilename=JarvisSetup
Compression=lzma
SolidCompression=yes
PrivilegesRequired=admin

[Files]
Source: "dist\Jarvis\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Jarvis"; Filename: "{app}\Jarvis.exe"
Name: "{commondesktop}\Jarvis"; Filename: "{app}\Jarvis.exe"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"