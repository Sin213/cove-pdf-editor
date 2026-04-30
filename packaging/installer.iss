; Inno Setup script for Cove PDF Editor (Windows)
; Invoked from build.ps1.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\cove-pdf-editor"
#endif
#ifndef OutputDir
  #define OutputDir "..\release"
#endif
#ifndef IconFile
  #define IconFile "..\cove_icon.ico"
#endif

[Setup]
AppId={{E7A8D6C4-9D51-4F8E-BB62-1C3D9F2A7B14}
AppName=Cove PDF Editor
AppVersion={#AppVersion}
AppPublisher=Cove
AppPublisherURL=https://github.com/Sin213/cove-pdf-editor
AppSupportURL=https://github.com/Sin213/cove-pdf-editor/issues
AppUpdatesURL=https://github.com/Sin213/cove-pdf-editor/releases
DefaultDirName={autopf}\Cove PDF Editor
DefaultGroupName=Cove PDF Editor
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\cove-pdf-editor.exe
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=Cove-PDF-Editor-{#AppVersion}-Setup
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
Name: "{group}\Cove PDF Editor"; Filename: "{app}\cove-pdf-editor.exe"
Name: "{group}\Uninstall Cove PDF Editor"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Cove PDF Editor"; Filename: "{app}\cove-pdf-editor.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\cove-pdf-editor.exe"; Description: "Launch Cove PDF Editor"; Flags: nowait postinstall skipifsilent
