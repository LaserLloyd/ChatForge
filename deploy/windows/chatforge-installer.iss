; ChatForge installer: an Inno Setup 6 wrapper around the same scripts the zip uses.
;
; STATUS: DORMANT. Nothing builds this file in CI and the README does not offer it as an install method.
; It has NOT been compiled (Inno Setup is not part of the project's tooling yet). Turn it on only when
; the project owns a code-signing certificate: an unsigned Setup.exe gets the same SmartScreen
; "unknown publisher" warning as the .bat files plus more antivirus false positives, so it adds
; nothing over the zip. See deploy\windows\README.md ("A signed Setup.exe, later").
;
; What it does: shows the usual wizard, extracts the same payload as the zip to a temp folder, runs
; deploy\windows\Install-ChatForge.ps1 on it (per-user, no admin); uninstalling is the script's own
; Installed-apps entry (Uninstall-ChatForge.ps1, data kept). The scripts do all the real work, so
; the .exe and the zip can never disagree about what an install is.
;
; Build (Inno Setup 6, ISCC.exe), after building and extracting the zip:
;   powershell -ExecutionPolicy Bypass -File deploy\windows\build-release-zip.ps1
;   Expand-Archive dist\ChatForge-0.2.0-windows.zip dist\stage
;   ISCC.exe /DAppVersion=0.2.0 /DStageDir=dist\stage\ChatForge-0.2.0-windows deploy\windows\chatforge-installer.iss
; The result is dist\ChatForge-Setup-0.2.0.exe.

#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif
#ifndef StageDir
  #define StageDir "..\..\dist\stage\ChatForge-" + AppVersion + "-windows"
#endif

[Setup]
AppId={{6D3A1F98-FB9B-4DD9-8212-33D5EC65B461}
AppName=ChatForge
AppVersion={#AppVersion}
AppPublisher=ChatForge
AppPublisherURL=https://github.com/LaserLloyd/ChatForge
; No application folder of Inno's own: the app goes where the script puts it
; (%LOCALAPPDATA%\Programs\ChatForge), exactly as with the zip, and the script registers the
; Installed-apps entry whose uninstaller is the script's. So Inno only starts the install.
CreateAppDir=no
Uninstallable=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
OutputDir=..\..\dist
OutputBaseFilename=ChatForge-Setup-{#AppVersion}
; Signing: define the tool once in the Inno IDE (Tools > Configure Sign Tools) or with
; /Sname=command on the ISCC line, then uncomment the next line. Until then the .exe is unsigned.
;SignTool=chatforgesign $f

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "Start ChatForge when I sign in to Windows"; GroupDescription: "Options:"
Name: "desktopicon"; Description: "Put a ChatForge icon on the desktop"; GroupDescription: "Options:"

[Files]
; The same payload as the zip: the two scripts and the app source.
Source: "{#StageDir}\deploy\*"; DestDir: "{tmp}\chatforge\deploy"; Flags: recursesubdirs ignoreversion deleteafterinstall
Source: "{#StageDir}\app\*"; DestDir: "{tmp}\chatforge\app"; Flags: recursesubdirs ignoreversion deleteafterinstall

[Run]
; The Start menu entry and the Installed-apps entry are made by the script (-NoShortcuts is not passed);
; the desktop icon and autostart follow the tasks above.
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{tmp}\chatforge\deploy\windows\Install-ChatForge.ps1"" -SourceDir ""{tmp}\chatforge\app"" {code:ScriptFlags}"; \
  StatusMsg: "Installing ChatForge (downloads Python and the app's libraries the first time) ..."; \
  Flags: runhidden waituntilterminated

[Code]
function ScriptFlags(Param: String): String;
begin
  Result := '-Yes -NoLaunch';
  if not WizardIsTaskSelected('autostart') then
    Result := Result + ' -NoAutostart';
  if not WizardIsTaskSelected('desktopicon') then
    Result := Result + ' -NoDesktopIcon';
end;
