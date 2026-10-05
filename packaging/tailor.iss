; Inno Setup script: turns dist/SponsorJobs into SponsorJobsSetup.exe.
;
; Build (from the repo root, after `pyinstaller packaging/tailor.spec`):
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\tailor.iss
; Output: dist/installer/SponsorJobsSetup.exe
;
; What the person gets: one installer, a Start-menu entry, an optional desktop icon, and
; an entry in Add/Remove Programs. No terminal, no Python, no "clone the repo".

#define AppName        "SponsorJobs"
#define AppVersion     "0.1.0"
#define AppPublisher   "Black Origin"
#define AppExeName     "SponsorJobs.exe"

[Setup]
AppId={{8E5A1C74-3B2D-4E96-9A1F-7C6B0D2E4F13}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Per-user install: no admin prompt, and it keeps the app out of Program Files, which
; matters because a UAC prompt on first run is where a nervous first-time user quits.
PrivilegesRequired=lowest
OutputDir=..\dist\installer
OutputBaseFilename=SponsorJobsSetup
SetupIconFile=..\ui\static\icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The app itself is 64-bit Python; don't offer to install it on 32-bit Windows.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; The whole PyInstaller folder. recursesubdirs pulls in _internal, which is where the CV
; templates, the manifests and the web UI live: without it the app installs and cannot
; render a single page.
Source: "..\dist\SponsorJobs\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The browser extension. It is HALF the product: LinkedIn and Indeed don't permit bots
; (§7), so the extension is how we help there, running in the person's own browser with the
; person driving. Shipping the app without it meant it existed in the repo and reached
; nobody. Chrome CANNOT be made to install an unpacked extension from an installer (a
; deliberate Chrome security rule, not a gap here), so we install the folder and the
; shortcut below opens it. The real fix is a Web Store listing at launch: free, because
; Google killed extension payments in 2021, so every competitor's extension is free and
; the paywall lives in the app.
Source: "..\extension\*"; DestDir: "{app}\extension"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
; A way back to the extension folder. Without this the person has to be told a file path,
; and "browse to C:\Users\...\AppData\Local\Programs\SponsorJobs\extension" is where a
; non-technical user gives up.
Name: "{group}\Browser extension (for LinkedIn)"; Filename: "{app}\extension"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Open SponsorJobs"; Flags: nowait postinstall skipifsilent

; NOTE ON DATA: nothing here writes to {app} at runtime. The person's profile, CVs and API
; key live in %LOCALAPPDATA%\SponsorJobs (see packaging/launcher.py), so uninstalling or
; updating the app never touches anything they own. The uninstaller deliberately leaves
; that folder alone.

[Code]
// SponsorJobs renders CVs with LaTeX and cannot make a single PDF without it. Checked BEFORE
// installing rather than after: finding out at first run, when the person is trying to
// build their first CV, is the worst possible moment to learn about a prerequisite.
// This does not block the install (they may install TeX later, or have it somewhere
// unusual); it tells them plainly, once, while they are still in a mood to fix it.
function LatexFound(): Boolean;
var
  Path: String;
begin
  Result := False;
  if FileExists(ExpandConstant('{localappdata}\Programs\MiKTeX\miktex\bin\x64\pdflatex.exe')) then
    Result := True
  else if FileExists('C:\Program Files\MiKTeX\miktex\bin\x64\pdflatex.exe') then
    Result := True
  else if RegQueryStringValue(HKLM, 'SOFTWARE\MiKTeX.org\MiKTeX', 'Path', Path) then
    Result := True
  else if FileExists('C:\texlive\2026\bin\windows\pdflatex.exe') then
    Result := True
  else if FileExists('C:\texlive\2025\bin\windows\pdflatex.exe') then
    Result := True;
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not LatexFound() then
  begin
    if MsgBox('SponsorJobs uses LaTeX to typeset your CV, and it looks like LaTeX is not installed yet.'
      + #13#10#13#10 'You can continue and install it afterwards from miktex.org, but SponsorJobs will not'
      + ' be able to produce a PDF until you do.'
      + #13#10#13#10 'Continue anyway?', mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
  end;
end;
