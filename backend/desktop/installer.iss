; Inno Setup script for FIRELENS (Windows installer). Built by desktop\build.ps1, or:
;   ISCC.exe /DAppVersion=1.0.0 desktop\installer.iss      (from backend\, after PyInstaller)
; Installs per user (no administrator rights), with Start-menu and optional desktop shortcuts.
; The app's data stays in %LOCALAPPDATA%\FIRELENS and is kept on uninstall unless chosen otherwise.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{25C0EE7B-CE45-4E36-8248-6C66689774A0}
AppName=FIRELENS
AppVersion={#AppVersion}
AppVerName=FIRELENS {#AppVersion}
AppPublisher=ÆOn Software Solutions
AppComments=Satellite fire intelligence: NASA FIRMS MODIS and VIIRS on a 3D globe
DefaultDirName={localappdata}\Programs\FIRELENS
DefaultGroupName=FIRELENS
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=FIRELENS-Setup-{#AppVersion}
SetupIconFile=firelens.ico
UninstallDisplayIcon={app}\FIRELENS.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\FIRELENS\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\FIRELENS"; Filename: "{app}\FIRELENS.exe"
Name: "{group}\FIRELENS data folder"; Filename: "{localappdata}\FIRELENS"
Name: "{group}\{cm:UninstallProgram,FIRELENS}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\FIRELENS"; Filename: "{app}\FIRELENS.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\FIRELENS.exe"; Description: "{cm:LaunchProgram,FIRELENS}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/c taskkill /im FIRELENS.exe /f"; Flags: runhidden; RunOnceId: "StopFirelens"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    if DirExists(ExpandConstant('{localappdata}\FIRELENS')) then
      if MsgBox('Also delete FIRELENS''s downloaded data (fire database and map tiles)?',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(ExpandConstant('{localappdata}\FIRELENS'), True, True, True);
end;
