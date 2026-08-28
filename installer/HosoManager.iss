; Release identity is supplied by build_manager.ps1.  A candidate must never
; reuse a released version or installer filename.
#ifndef ReleaseVersion
  #error ReleaseVersion is required
#endif
#ifndef SourceDir
  #error SourceDir is required
#endif

[Setup]
AppId={{8E6B66E4-3C0A-4B4A-AB7F-CA67125A381E}
AppName=Hồ sơ Digitization Manager
AppVersion={#ReleaseVersion}
VersionInfoVersion=0.2.1.0
DefaultDirName={localappdata}\HosoManager
DefaultGroupName=Hồ sơ Digitization Manager
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=HosoManager-Setup-v{#ReleaseVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayName=Hồ sơ Digitization Manager {#ReleaseVersion}

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Hồ sơ Digitization Manager"; Filename: "{app}\HosoManager.exe"
Name: "{autodesktop}\Hồ sơ Digitization Manager"; Filename: "{app}\HosoManager.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop icon"; Flags: unchecked

[Run]
Filename: "{app}\HosoManager.exe"; Description: "Launch Hồ sơ Digitization Manager"; Flags: nowait postinstall skipifsilent