#define MyVer GetEnv("DHUB_VERSION")
#if MyVer == ""
  #undef MyVer
  #define MyVer "1.0"
#endif

[Setup]
AppId={{8F3C2A51-6D7B-4E0A-9C21-D4B7A1E5F3C9}
AppName=D-Hub
AppVersion={#MyVer}
AppPublisher=D-Hub
DefaultDirName={autopf}\D-Hub
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=output
OutputBaseFilename=D-Hub-Setup
SetupIconFile=logo.ico
UninstallDisplayIcon={app}\D-Hub.exe
LicenseFile=AGB.txt
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "Verknüpfung auf dem Desktop erstellen"; GroupDescription: "Zusätzliche Verknüpfungen:"
Name: "autostart"; Description: "D-Hub automatisch mit Windows starten"; GroupDescription: "Weitere Optionen:"; Flags: unchecked

[Files]
Source: "dist\D-Hub.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autodesktop}\D-Hub"; Filename: "{app}\D-Hub.exe"; Tasks: desktopicon
Name: "{autoprograms}\D-Hub"; Filename: "{app}\D-Hub.exe"
Name: "{autoprograms}\D-Hub deinstallieren"; Filename: "{uninstallexe}"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "D-Hub"; ValueData: """{app}\D-Hub.exe"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\D-Hub.exe"; Description: "D-Hub jetzt starten"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
procedure StopDHub();
var R: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im D-Hub.exe', '', SW_HIDE, ewWaitUntilTerminated, R);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopDHub();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  StopDHub();
  Result := True;
end;
