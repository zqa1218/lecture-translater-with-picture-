#Requires -Version 5.1
<#
.SYNOPSIS
  Build the single-file Windows exe for the PPT/PDF translator.

.DESCRIPTION
  Why an ASCII build workspace: PyQt5 (Qt 5) cannot handle non-ASCII paths on
  Windows - Qt reports its plugin directory as "C:/????/...", which breaks both
  running from source and PyInstaller's Qt hook. The project itself may live in a
  non-ASCII folder, but the *build* venv must be under an ASCII path.
  The produced exe is onefile, so at runtime it unpacks into %TEMP% (ASCII) and
  therefore works fine even when placed in a Chinese-named folder.

  ASCII-only source on purpose (PowerShell 5.1 uses the system code page without BOM).

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1 -SkipTests
#>
[CmdletBinding()]
param(
  [string]$Root = '',
  [string]$Name = 'ppt-pdf-translator',
  [string]$AsciiWork = 'C:\pptx-build',
  [switch]$SkipTests,
  [switch]$Onedir
)

$ErrorActionPreference = 'Stop'
function Write-Step($m) { Write-Host "[build] $m" -ForegroundColor Cyan }
function Write-Err2($m) { Write-Host "[build] $m" -ForegroundColor Red }

if (-not $Root) { $Root = $PSScriptRoot }
$src = Join-Path $Root 'src'
$distPath = Join-Path $Root 'work\dist'

# Build venv for the exe: always the ASCII one (Qt 5 cannot handle non-ASCII paths).
$asciiVenv = Join-Path $AsciiWork 'venv'
$asciiPy = Join-Path $asciiVenv 'Scripts\python.exe'
$asciiPyI = Join-Path $asciiVenv 'Scripts\pyinstaller.exe'
$asciiSrc = Join-Path $AsciiWork 'src'

# Tests run with the project venv when it exists, otherwise with the ASCII build venv
# (this repository builds straight out of that one).
$projectVenvPy = Join-Path $Root 'work\venv\Scripts\python.exe'
if (-not (Test-Path $projectVenvPy)) { $projectVenvPy = $asciiPy }

# Locate a base interpreter without hardcoding non-ASCII paths: a venv records
# its own base in pyvenv.cfg ("home = ...").
function Find-BasePython {
  foreach ($cfg in @((Join-Path $Root 'work\venv\pyvenv.cfg'), (Join-Path $asciiVenv 'pyvenv.cfg'))) {
    if (-not (Test-Path $cfg)) { continue }
    $line = Select-String -Path $cfg -Pattern '^home\s*=\s*(.+)$' | Select-Object -First 1
    if ($line) {
      $candidate = Join-Path $line.Matches[0].Groups[1].Value.Trim() 'python.exe'
      if (Test-Path $candidate) { return $candidate }
    }
  }
  $sibling = Join-Path (Split-Path -Parent $Root) 'btb-custom\work\python311\python.exe'
  if (Test-Path $sibling) { return $sibling }
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  return ''
}

if (-not $SkipTests) {
  if (Test-Path $projectVenvPy) {
    Write-Step 'running test suite'
    Push-Location $Root
    & $projectVenvPy -m pytest tests -q
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -ne 0) { Write-Err2 ('tests failed (exit ' + $code + '); aborting build'); exit 3 }
    Write-Step 'tests passed'
  } else {
    Write-Err2 'no interpreter found for tests; skipping (use -SkipTests to silence)'
  }
}


# ---------------------------------------------------------------- ASCII workspace
New-Item -ItemType Directory -Force -Path $AsciiWork | Out-Null

if (-not (Test-Path $asciiPy)) {
  $basePython = Find-BasePython
  if (-not $basePython) { Write-Err2 'cannot locate a base python interpreter'; exit 2 }
  Write-Step "creating ASCII venv at $asciiVenv (base: $basePython)"
  & $basePython -m venv $asciiVenv
}

$mirrors = @(
  'https://pypi.tuna.tsinghua.edu.cn/simple',
  'https://mirrors.aliyun.com/pypi/simple'
)
function Install-Pkg([string]$pkg) {
  # PowerShell 5.1 treats a native command's stderr as a terminating error when
  # $ErrorActionPreference = 'Stop'; pip writes progress/warnings to stderr, so
  # relax it locally and rely on the exit code instead.
  $previous = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    foreach ($m in $mirrors) {
      & $asciiPy -m pip install $pkg -q --index-url $m 2>&1 | Out-Null
      if ($LASTEXITCODE -eq 0) { return $true }
    }
    return $false
  } finally {
    $ErrorActionPreference = $previous
  }
}

$packages = @(
  'pyinstaller', 'PyQt5', 'python-pptx', 'pymupdf',
  'rapidocr-onnxruntime', 'opencv-python-headless<5',
  'faster-whisper', 'python-docx', 'mutagen', 'pillow'
)
foreach ($pkg in $packages) {
  Write-Step "ensuring $pkg in ASCII venv"
  if (-not (Install-Pkg $pkg)) { Write-Err2 "failed to install $pkg"; exit 3 }
}

# verify Qt can be located from the ASCII path (this is exactly what broke before)
Write-Step 'sanity check: Qt plugin dir resolution from ASCII path'
& $asciiPy -c "from PyQt5.QtCore import QLibraryInfo; import os; p=QLibraryInfo.location(QLibraryInfo.PluginsPath); print('PluginsPath=', p); print('qwindows exists=', os.path.exists(os.path.join(p,'platforms','qwindows.dll')))"

# ---------------------------------------------------------------- copy sources
Write-Step "copying sources to $asciiSrc"
if (Test-Path $asciiSrc) { Remove-Item $asciiSrc -Recurse -Force }
Copy-Item $src $asciiSrc -Recurse -Force

# ---------------------------------------------------------------- build
$workPath = Join-Path $AsciiWork 'pyi-build'
$asciiDist = Join-Path $AsciiWork 'dist'
if (Test-Path $workPath) { Remove-Item $workPath -Recurse -Force }
if (Test-Path $asciiDist) { Remove-Item $asciiDist -Recurse -Force }

if ($Onedir) {
  $layoutArgs = @('--onedir')
  $layoutLabel = 'onedir'
} else {
  $layoutArgs = @('--onefile')
  $layoutLabel = 'onefile'
}
# Importing PyQt5 before onnxruntime triggers access violation 0xC0000005 (see the
# note at the top of main.py). PyInstaller's isolated subprocess imports the
# collected packages in order (PyQt5 first, onnxruntime later) and crashes inside
# find_binary_dependencies (SubprocessDiedError). Drop a temporary sitecustomize.py
# into site-packages so every interpreter of this venv imports onnxruntime first -
# the same workaround main.py uses at runtime - and restore it when the build ends.
# NOTE: do not add --collect-all onnxruntime either; it imports
# onnxruntime.quantization, which needs the (absent) onnx module and kills the
# isolated subprocess. hooks-contrib's hook-onnxruntime already collects capi DLLs.
# Keep this file ASCII-only: Windows PowerShell 5.1 reads BOM-less .ps1 with the
# system code page, so non-ASCII comments silently corrupt the following code.
$siteCustomize = Join-Path $asciiVenv 'Lib\site-packages\sitecustomize.py'
$siteCustomizeBackup = $null
if (Test-Path $siteCustomize) { $siteCustomizeBackup = [IO.File]::ReadAllText($siteCustomize) }
$siteCustomizeBody = @(
  '# temporary shim written by build.ps1 - imported by every python of this venv',
  'try:',
  '    import onnxruntime  # noqa: F401',
  'except Exception:',
  '    pass'
) -join [Environment]::NewLine
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $siteCustomize) | Out-Null
[IO.File]::WriteAllText($siteCustomize, $siteCustomizeBody)
# PyQt5's wheel ships the VS2017 MSVC runtime (14.26) while onnxruntime / opencv /
# ctranslate2 are built with VS2022 and need 14.4x. Inside one frozen app Windows
# loads whichever copy comes first, and the newer components then die with
# 0xC0000005 at startup. 14.x is backward compatible, so temporarily refresh the
# copies inside PyQt5\Qt5\bin with the newest runtime on this machine, and put the
# originals back when the build is done.
$qtBin = Join-Path $asciiVenv 'Lib\site-packages\PyQt5\Qt5\bin'
$runtimeBackup = @{}
foreach ($dll in @('msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'concrt140.dll')) {
  $systemDll = Join-Path $env:SystemRoot ('System32\' + $dll)
  $bundledDll = Join-Path $qtBin $dll
  if ((Test-Path $systemDll) -and (Test-Path $bundledDll)) {
    $runtimeBackup[$bundledDll] = [IO.File]::ReadAllBytes($bundledDll)
    Copy-Item $systemDll $bundledDll -Force
  }
}
Write-Step ('refreshed ' + $runtimeBackup.Count + ' bundled MSVC runtime dll(s) from System32')

Write-Step ('pyinstaller ' + $layoutLabel + ' build (a few minutes)')
Push-Location $asciiSrc
try {
  & $asciiPyI `
    --noconfirm `
    $layoutArgs `
    --windowed `
    --name $Name `
    --workpath $workPath `
    --distpath $asciiDist `
    --specpath $workPath `
    --collect-data pptx `
    --collect-all pymupdf `
    --collect-data docx `
    --collect-all rapidocr_onnxruntime `
    --collect-all cv2 `
    --collect-all ctranslate2 `
    --collect-all tokenizers `
    --collect-all av `
    --hidden-import pptx `
    --hidden-import rapidocr_onnxruntime `
    --collect-submodules faster_whisper `
    --exclude-module tkinter `
    --exclude-module matplotlib `
    main.py
  $code = $LASTEXITCODE
} finally {
  Pop-Location
  if ($null -ne $siteCustomizeBackup) {
    [IO.File]::WriteAllText($siteCustomize, $siteCustomizeBackup)
  } else {
    Remove-Item -LiteralPath $siteCustomize -Force -ErrorAction SilentlyContinue
  }
  foreach ($entry in $runtimeBackup.GetEnumerator()) {
    [IO.File]::WriteAllBytes($entry.Key, $entry.Value)
  }
}

if ($Onedir) { $builtExe = Join-Path $asciiDist "$Name\$Name.exe" } else { $builtExe = Join-Path $asciiDist "$Name.exe" }
if ($code -ne 0 -or -not (Test-Path $builtExe)) { Write-Err2 "build failed (exit $code)"; exit 4 }

New-Item -ItemType Directory -Force -Path $distPath | Out-Null
if ($Onedir) { $finalExe = Join-Path $distPath "$Name\$Name.exe" } else { $finalExe = Join-Path $distPath "$Name.exe" }
if ($Onedir) {
  Copy-Item (Split-Path -Parent $builtExe) (Split-Path -Parent $finalExe) -Recurse -Force
} else {
  Copy-Item $builtExe $finalExe -Force
}

$item = Get-Item $finalExe
$hash = (Get-FileHash -LiteralPath $finalExe -Algorithm SHA256).Hash
Write-Step ("built: {0}  ({1:N0} bytes / {2:N1} MB)" -f $item.FullName, $item.Length, ($item.Length / 1MB))
Write-Step "sha256: $hash"

$bytes = [IO.File]::ReadAllBytes($finalExe)
$peOffset = [BitConverter]::ToInt32($bytes, 0x3C)
$subsystem = [BitConverter]::ToUInt16($bytes, $peOffset + 0x5C)
Write-Step "PE subsystem: $subsystem  (2 = GUI/no console, 3 = console)"


