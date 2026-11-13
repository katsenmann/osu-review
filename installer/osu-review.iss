; osu!review installer (Inno Setup 6). Built by .github/workflows/build-installer.yml:
;   build\osu-review\  = program files + its own Python 3.12 with pywebview (python\)
;   iscc /DMyVersion=1.0.0-beta.3 installer\osu-review.iss
; Installs per user (no admin), into %LOCALAPPDATA%\Programs\osu-review.
; Scores and settings stay in %APPDATA%\osu-review and are only removed when the user says so.

#ifndef MyVersion
  #define MyVersion "0.0.0-dev"
#endif
#ifndef MyNumVersion
  #define MyNumVersion "0.0.0"
#endif

[Setup]
AppId={{54A5B7A4-045B-479B-8BDF-0B3D5D4D1278}
AppName=osu!review
AppVersion={#MyVersion}
AppVerName=osu!review {#MyVersion}
AppPublisher=Katsenmann
AppPublisherURL=https://github.com/katsenmann/osu-review
AppSupportURL=https://github.com/katsenmann/osu-review/issues
AppUpdatesURL=https://github.com/katsenmann/osu-review/releases/latest
DefaultDirName={localappdata}\Programs\osu-review
DisableDirPage=auto
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=osu-review-Setup
VersionInfoVersion={#MyNumVersion}
VersionInfoProductVersion={#MyNumVersion}
VersionInfoProductTextVersion={#MyVersion}
VersionInfoDescription=osu!review Setup
SetupIconFile=..\web\icon.ico
UninstallDisplayIcon={app}\web\icon.ico
UninstallDisplayName=osu!review
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "de"; MessagesFile: "compiler:Languages\German.isl"

[CustomMessages]
en.Shortcuts=Shortcuts:
de.Shortcuts=Verknüpfungen:
en.DesktopIcon=Create a desktop shortcut
de.DesktopIcon=Verknüpfung auf dem Desktop erstellen
en.AutoStart=Start osu!review with Windows (waits quietly in the tray next to the clock)
de.AutoStart=osu!review mit Windows starten (wartet still im Tray neben der Uhr)
en.StartNow=Start osu!review now
de.StartNow=osu!review jetzt starten
en.AppComment=Replay analysis right after every osu! score
de.AppComment=Replay-Analyse direkt nach jedem osu!-Score
en.AskData=Also delete your analysed scores and settings?%n%nChoose "No" to keep them for a later reinstall.
de.AskData=Auch deine analysierten Scores und Einstellungen löschen?%n%nWähle „Nein“, um sie für eine spätere Neuinstallation zu behalten.

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"; GroupDescription: "{cm:Shortcuts}"
Name: "autostart"; Description: "{cm:AutoStart}"; GroupDescription: "{cm:Shortcuts}"; Flags: unchecked

[InstallDelete]
; shortcuts of the old ZIP install (Install.bat) - recreated below if wanted
Type: files; Name: "{userdesktop}\osu!review.lnk"
Type: files; Name: "{userstartup}\osu!review.lnk"

[Files]
Source: "..\build\osu-review\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{userprograms}\osu!review"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app.py"""; WorkingDir: "{app}"; IconFilename: "{app}\web\icon.ico"; Comment: "{cm:AppComment}"
Name: "{userdesktop}\osu!review"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app.py"""; WorkingDir: "{app}"; IconFilename: "{app}\web\icon.ico"; Tasks: desktopicon
Name: "{userstartup}\osu!review"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app.py"" --no-window"; WorkingDir: "{app}"; IconFilename: "{app}\web\icon.ico"; Tasks: autostart

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app.py"""; WorkingDir: "{app}"; Description: "{cm:StartNow}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; caches, backups of updates and anything the updater added
Type: filesandordirs; Name: "{app}"

[Code]
procedure StopRunning();
var
  Code: Integer;
begin
  // ask a running osu!review to quit (window, tray icon, background part), then give it a moment
  Exec('powershell.exe',
    '-NoProfile -WindowStyle Hidden -Command "try { Invoke-WebRequest -UseBasicParsing -Method Post -TimeoutSec 3 http://127.0.0.1:47290/api/quit | Out-Null; Start-Sleep -Seconds 2 } catch {}"',
    '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunning();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  StopRunning();
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Data := ExpandConstant('{userappdata}\osu-review');
    if DirExists(Data) and not UninstallSilent() then
      if MsgBox(ExpandConstant('{cm:AskData}') + #13#10 + Data,
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Data, True, True, True);
  end;
end;
