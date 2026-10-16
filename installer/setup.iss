; PPT 课件学习助手 —— 一键安装包脚本（Inno Setup 6）
;
; 用法（在仓库根目录执行）：
;   1) powershell -File .\build.ps1                 ; 先构建单文件 exe
;   2) 把 work\dist\ppt-pdf-translator.exe 复制成 work\dist\PPT课件学习助手.exe（或改下面的 Payload）
;   3) ISCC.exe installer\setup.iss                  ; 产物在 installer\dist\
;
; 可选：把 Inno Setup 的简体中文语言文件放在 installer\ChineseSimplified.isl，
; 安装向导会自动变中文；没有该文件时只用英文界面，仍能正常安装。

#ifndef Payload
  #define Payload "..\work\dist\ppt-pdf-translator.exe"
#endif
#ifndef OutDir
  #define OutDir "dist"
#endif
#ifndef AppVersion
  #define AppVersion "2.2"
#endif

#define AppName "PPT课件学习助手"
#define AppExeName "PPT课件学习助手.exe"
#define AppUrl "https://github.com/zqa1218/lecture-translater-with-picture-"

[Setup]
AppId={{D3B7A1C4-0E52-4A7F-9E31-5C7A64B2F018}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=zqa1218
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}
AppUpdatesURL={#AppUrl}/releases
VersionInfoVersion={#AppVersion}.0.0
VersionInfoDescription={#AppName} 安装程序
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes
; 装到用户目录，不需要管理员权限
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutDir}
OutputBaseFilename=PPT-Lecture-Translator-v{#AppVersion}-Setup
; 单文件 exe 已是压缩体（PyInstaller onefile），再压一遍收益很小，用 fast 省时间
Compression=lzma2/fast
SolidCompression=no
WizardStyle=modern
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
CloseApplications=no
SetupLogging=yes

[Languages]
#if FileExists(AddBackslash(SourcePath) + "ChineseSimplified.isl")
Name: "chinese"; MessagesFile: "ChineseSimplified.isl"
#endif
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
Source: "{#Payload}"; DestDir: "{app}"; DestName: "{#AppExeName}"; Flags: ignoreversion
Source: "使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\使用说明.txt"; Description: "查看使用说明"; Flags: shellexec postinstall skipifsilent unchecked