; DedSec CCTV - Inno Setup 6 installer (modern wizard)
; Build with:  powershell -File installer\build_installer.ps1
; Or:          ISCC.exe /DAppVersion=1.0.2 installer\DedSecCCTV.iss
;
; Expects a Nuitka standalone folder next to installer\:
;   ..\DedSecCCTV\DedSecCCTV.exe  (+ DedSecUpdater.exe recommended)

#ifndef AppVersion
  #include "version.inc"
#endif

#define MyAppName       "DedSec CCTV"
#define MyAppPublisher  "DedSec Lab"
#define MyAppURL        "https://github.com/QuacksOnWelfare/DedSecCCTV"
#define MyAppSupportURL "https://discord.com/invite/4gRcYtTQ5c"
#define MyAppExeName    "DedSecCCTV.exe"
#define MyAppMutex      "DedSecCCTV_SingleInstance"
; Fixed GUID - do not change after the first public release.
#define MyAppGuid       "E4B9A653-DDE6-59D4-959B-D211C8E5E91F"
#define MyAppId         "{{E4B9A653-DDE6-59D4-959B-D211C8E5E91F}"
; Registry uninstall key name (includes braces; used only in [Code] strings).
#define MyAppUninstId    "{E4B9A653-DDE6-59D4-959B-D211C8E5E91F}_is1"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppVerName={#MyAppName} {#AppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppSupportURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Terms of use (TERMS.txt) + MIT licence on one must-accept page. Rebuild LICENSE_AND_TERMS.txt if either changes.
LicenseFile=LICENSE_AND_TERMS.txt
InfoBeforeFile=InfoBefore.txt
OutputDir=Output
OutputBaseFilename=DedSecCCTV-Setup-{#AppVersion}
SetupIconFile=assets\dedsec_logo.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
WizardStyle=modern
WizardSizePercent=120
WizardImageFile=assets\wizard_image.png,assets\wizard_image@2x.png
WizardSmallImageFile=assets\wizard_small.png,assets\wizard_small@2x.png
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
PrivilegesRequired=lowest
; per-user only, no install-mode dialog
; Per-user install under %LOCALAPPDATA% so the auto-updater can replace files without elevation.
UsePreviousAppDir=yes
UsePreviousTasks=yes
UsePreviousGroup=yes
AllowNoIcons=yes
CloseApplications=yes
CloseApplicationsFilter=DedSecCCTV.exe,DedSecUpdater.exe
RestartApplications=no
AppMutex={#MyAppMutex}
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Setup
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#AppVersion}
VersionInfoCopyright=Copyright (C) 2026 {#MyAppPublisher}
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
SetupAppTitle=DedSec CCTV Setup
SetupWindowTitle=DedSec CCTV Setup - %1
WelcomeLabel1=Welcome to DedSec CCTV Setup
WelcomeLabel2=This will install [name/ver] on your computer.%n%nIt is recommended that you close DedSec CCTV and other applications before continuing.%n%nClick Next to continue, or Cancel to exit Setup.
FinishedHeadingLabel=Completing DedSec CCTV Setup
FinishedLabelNoIcons=Setup has finished installing DedSec CCTV on your computer.
FinishedLabel=Setup has finished installing DedSec CCTV on your computer. The application may be launched by selecting the installed shortcuts.
ClickFinish=Click Finish to exit Setup.
WizardLicense=Terms of use and licence
LicenseLabel=Please read the terms of use and licence before continuing.
LicenseLabel3=Please read the following terms of use and licence. You must accept them before continuing with the installation.
LicenseAccepted=I &accept the terms of use and licence
LicenseNotAccepted=I &do not accept
InfoBeforeLabel=Please read the following information before continuing.
InfoBeforeClickLabel=When you are ready to continue with Setup, click Next.
ConfirmUninstall=Are you sure you want to completely remove %1 and all of its components?
UninstalledAll=%1 was successfully removed from your computer.

[CustomMessages]
CreateDesktopIcon=Create a &desktop shortcut
LaunchApp=Launch DedSec CCTV
RemoveUserData=Also remove my DedSec CCTV settings and local data?
RemoveUserDataNote=This deletes saved streams, wall layout, and banners under AppData.%nSign-in credentials stored by Windows Credential Manager are left alone.
VpnPageTitle=Protect your connection
VpnPageSub=A quick privacy tip before you install.
VpnPageText=We recommend using a VPN while you use DedSec CCTV, so your home IP address isn't shown to the camera servers you connect to.
VpnOpenWindscribe=Open the Windscribe download page after setup (free VPN)
VpnAnyVpn=Any reputable VPN works, for example Windscribe or Proton VPN. Nothing is installed for you.
VpnLinkWindscribe=windscribe.com/download
VpnLinkProton=protonvpn.com/download
NewerAlreadyInstalled=A newer version of DedSec CCTV (%1) is already installed. Setup will now exit.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
; Entire Nuitka standalone dist (exe, DLLs, assets, optional DedSecUpdater.exe).
Source: "..\DedSecCCTV\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent; WorkingDir: "{app}"

[UninstallDelete]
; Leftover files created at runtime next to the exe (never touch AppData here).
Type: filesandordirs; Name: "{app}\user_streams"
Type: files; Name: "{app}\*.log"

[Code]
const
  WindscribeURL = 'https://windscribe.com/download';
  ProtonURL = 'https://protonvpn.com/download';
  UninstKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#MyAppUninstId}';
  AppDataDirName = 'DedSecCctv';

function GetInstalledVersion(var Version: String): Boolean;
begin
  Result := RegQueryStringValue(HKCU, UninstKey, 'DisplayVersion', Version);
  if not Result then
    Result := RegQueryStringValue(HKLM, UninstKey, 'DisplayVersion', Version);
end;

function CompareVersionParts(const A, B: String): Integer;
var
  VA, VB: Int64;
begin
  if not StrToVersion(A, VA) then
  begin
    Result := 0;
    Exit;
  end;
  if not StrToVersion(B, VB) then
  begin
    Result := 0;
    Exit;
  end;
  Result := ComparePackedVersion(VA, VB);
end;

var
  VpnPage: TInputOptionWizardPage;

procedure OpenUrl(const Url: String);
var
  ErrorCode: Integer;
begin
  ShellExec('open', Url, '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
end;

procedure WindscribeLinkClick(Sender: TObject);
begin
  OpenUrl(WindscribeURL);
end;

procedure ProtonLinkClick(Sender: TObject);
begin
  OpenUrl(ProtonURL);
end;

function MakeLink(const Caption: String; Top, Left: Integer; Handler: TNotifyEvent): TNewStaticText;
begin
  Result := TNewStaticText.Create(VpnPage);
  Result.Parent := VpnPage.Surface;
  Result.Caption := Caption;
  Result.Top := Top;
  Result.Left := Left;
  Result.Cursor := crHand;
  Result.Font.Color := $90740E;  { brand teal-dim #0e7490 (BGR), readable on white }
  Result.Font.Style := [fsUnderline];
  Result.OnClick := Handler;
end;

procedure InitializeWizard();
var
  Note: TNewStaticText;
  L1: TNewStaticText;
begin
  { 'Protect your connection' comes right after 'Before you start' (InfoBefore). }
  VpnPage := CreateInputOptionPage(wpInfoBefore,
    CustomMessage('VpnPageTitle'), CustomMessage('VpnPageSub'),
    CustomMessage('VpnPageText'), False, False);
  VpnPage.Add(CustomMessage('VpnOpenWindscribe'));
  VpnPage.Values[0] := False;  { unchecked by default; nothing is downloaded or installed }

  Note := TNewStaticText.Create(VpnPage);
  Note.Parent := VpnPage.Surface;
  Note.AutoSize := False;
  Note.WordWrap := True;
  Note.Width := VpnPage.SurfaceWidth;
  Note.Height := ScaleY(32);
  Note.Top := VpnPage.CheckListBox.Top + ScaleY(40);
  Note.Caption := CustomMessage('VpnAnyVpn');
  Note.Font.Color := $A08C78;  { muted #788ca0 }
  VpnPage.CheckListBox.Height := ScaleY(32);

  L1 := MakeLink(CustomMessage('VpnLinkWindscribe'), Note.Top + Note.Height + ScaleY(6), 0, @WindscribeLinkClick);
  MakeLink(CustomMessage('VpnLinkProton'), L1.Top, L1.Left + L1.Width + ScaleX(24), @ProtonLinkClick);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  { Only opens the official page in the browser, and only if the box was ticked. }
  if (CurStep = ssPostInstall) and (not WizardSilent) and (VpnPage <> nil) and VpnPage.Values[0] then
    OpenUrl(WindscribeURL);
end;

function InitializeSetup(): Boolean;
var
  Installed: String;
begin
  Result := True;
  if GetInstalledVersion(Installed) then
  begin
    if CompareVersionParts(Installed, '{#AppVersion}') > 0 then
    begin
      MsgBox(FmtMessage(CustomMessage('NewerAlreadyInstalled'), [Installed]),
        mbInformation, MB_OK);
      Result := False;
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  AppDataPath: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;

  AppDataPath := ExpandConstant('{userappdata}\' + AppDataDirName);
  if not DirExists(AppDataPath) then
    Exit;

  if MsgBox(
       CustomMessage('RemoveUserData') + #13#10#13#10 + CustomMessage('RemoveUserDataNote'),
       mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
  begin
    DelTree(AppDataPath, True, True, True);
  end;
  { Windows Credential Manager entries (keyring) are intentionally left alone.
    The app stores tokens under DedSecCCTV-auth / DedSecCCTV-login. }
end;
