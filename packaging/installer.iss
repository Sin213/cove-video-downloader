; Inno Setup script for Cove Video Downloader (Windows)
; Invoked from build.ps1 via:
;   iscc /DAppVersion=X.Y.Z /DSourceDir=<abs dist\cove-video-downloader> \
;        /DOutputDir=<abs release> /DIconFile=<abs cove_icon.ico> installer.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\cove-video-downloader"
#endif
#ifndef OutputDir
  #define OutputDir "..\release"
#endif
#ifndef IconFile
  #define IconFile "..\cove_icon.ico"
#endif

[Setup]
AppId={{F4C71D38-5B2A-4FA1-9B27-0D62E7C4F3A8}
AppName=Cove Video Downloader
AppVersion={#AppVersion}
AppPublisher=Cove
AppPublisherURL=https://github.com/Sin213/cove-video-downloader
AppSupportURL=https://github.com/Sin213/cove-video-downloader/issues
AppUpdatesURL=https://github.com/Sin213/cove-video-downloader/releases
DefaultDirName={autopf}\Cove Video Downloader
DefaultGroupName=Cove Video Downloader
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\cove-video-downloader.exe
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=cove-video-downloader-{#AppVersion}-Setup
SetupIconFile={#IconFile}
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Cove Video Downloader"; Filename: "{app}\cove-video-downloader.exe"
Name: "{group}\Uninstall Cove Video Downloader"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Cove Video Downloader"; Filename: "{app}\cove-video-downloader.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\cove-video-downloader.exe"; Description: "Launch Cove Video Downloader"; Flags: nowait postinstall skipifsilent
