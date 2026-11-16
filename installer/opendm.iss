; Inno Setup script for OpenDM. Built by:  .venv\Scripts\python tools/build_exe.py --installer
; (needs Inno Setup 6: winget install JRSoftware.InnoSetup)
;
; Installs per user (no admin prompt) into %LOCALAPPDATA%\Programs\OpenDM.
; Settings and the download list live in %LOCALAPPDATA%\OpenDM and are kept on uninstall.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Root
  #define Root ".."
#endif

[Setup]
AppId={{8F3C2B7E-5A41-4C1D-9E8B-3D6F0A2C7B19}
AppName=OpenDM
AppVersion={#AppVersion}
AppVerName=OpenDM {#AppVersion}
AppPublisher=OpenDM contributors
AppPublisherURL=https://github.com/yahyakhan234/opendm
AppSupportURL=https://github.com/yahyakhan234/opendm/issues
AppUpdatesURL=https://github.com/yahyakhan234/opendm/releases
DefaultDirName={autopf}\OpenDM
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
LicenseFile={#Root}\LICENSE
OutputDir={#Root}\dist
OutputBaseFilename=OpenDM-Setup-{#AppVersion}
SetupIconFile={#Root}\build\opendm.ico
UninstallDisplayIcon={app}\OpenDM.exe
UninstallDisplayName=OpenDM
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; OpenDM holds this mutex while running, so Setup and the uninstaller ask you to exit it first
; (right-click the tray icon > Exit). Closing the window only hides it to the tray.
AppMutex=OpenDM-Running
CloseApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
VersionInfoVersion={#AppVersion}
VersionInfoDescription=OpenDM download manager setup

[Tasks]
Name: "startup"; Description: "Start OpenDM when Windows starts (it waits in the system tray)"; GroupDescription: "Options:"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Remove the previous version's files first so nothing stale is left behind on upgrade.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#Root}\dist\OpenDM\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\OpenDM"; Filename: "{app}\OpenDM.exe"
Name: "{autodesktop}\OpenDM"; Filename: "{app}\OpenDM.exe"; Tasks: desktopicon
Name: "{userstartup}\OpenDM"; Filename: "{app}\OpenDM.exe"; Parameters: "--tray"; Tasks: startup

[Run]
Filename: "{app}\OpenDM.exe"; Description: "{cm:LaunchProgram,OpenDM}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; OpenDM's own copy of yt-dlp (it updates itself, so it lives in the data folder).
; Your settings and download list next to it are kept.
Type: filesandordirs; Name: "{localappdata}\OpenDM\tools"
