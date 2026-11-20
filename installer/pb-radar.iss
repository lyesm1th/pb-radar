; PB Radar per-user Windows setup. Only the stable launcher is installed here.
; OSM, the contact database and downloaded app versions live separately in
; %LOCALAPPDATA%\PB-Radar and are NEVER deleted by uninstalling this setup.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{A4D2F092-747B-4C21-AC1A-573A46FCD7B0}
AppName=PB Radar
AppVersion={#MyAppVersion}
AppVerName=PB Radar {#MyAppVersion}
AppPublisher=Polski Bramy
AppPublisherURL=https://github.com/lyesm1th/pb-radar
AppSupportURL=https://github.com/lyesm1th/pb-radar/issues
DefaultDirName={userpf}\PB Radar
DefaultGroupName=PB Radar
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
MinVersion=10.0
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
OutputDir=..\dist
OutputBaseFilename=PB-Radar-Setup
SetupIconFile=..\dist\PB-Radar.ico
UninstallDisplayIcon={app}\PB-Radar-Launcher.exe
CloseApplications=yes

[Languages]
Name: "ukrainian"; MessagesFile: "compiler:Languages\Ukrainian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Створити ярлик на робочому столі"; GroupDescription: "Ярлики:"; Flags: checkedonce

[Dirs]
Name: "{localappdata}\PB-Radar"; Flags: uninsneveruninstall
Name: "{localappdata}\PB-Radar\cache"; Flags: uninsneveruninstall
Name: "{localappdata}\PB-Radar\versions"; Flags: uninsneveruninstall

[Files]
Source: "..\dist\PB-Radar-Launcher.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\PB Radar"; Filename: "{app}\PB-Radar-Launcher.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\PB Radar"; Filename: "{app}\PB-Radar-Launcher.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\PB-Radar-Launcher.exe"; Description: "Запустити PB Radar"; Flags: nowait postinstall skipifsilent
