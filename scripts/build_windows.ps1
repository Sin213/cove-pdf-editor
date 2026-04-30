<#
.SYNOPSIS
    Build Cove PDF Editor Windows portable (onefile).
    Output: release\Cove-PDF-Editor-<Version>-Portable.exe
#>

[CmdletBinding()]
param(
    [string]$Version = "2.0.0"
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$AppDisplay  = "Cove PDF Editor"
$AppSlug     = "Cove-PDF-Editor"
$ReleaseDir  = "release"

function Step([string]$msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

Step "Building $AppDisplay v$Version (portable)"

Step "[1/4] Creating build venv"
if (Test-Path .buildenv) { Remove-Item -Recurse -Force .buildenv }
python -m venv .buildenv
& .\.buildenv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .\.buildenv\Scripts\python.exe -m pip install --quiet `
    PySide6 pymupdf pypdfium2 Pillow pyinstaller

Step "[2/4] Generating cove_icon.ico"
& .\.buildenv\Scripts\python.exe -c @"
from PIL import Image
Image.open('cove_icon.png').save(
    'cove_icon.ico',
    sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)],
)
"@

Step "[3/4] PyInstaller (onefile)"
if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist)  { Remove-Item -Recurse -Force dist  }

$portableName = "$AppSlug-$Version-Portable"

& .\.buildenv\Scripts\pyinstaller.exe `
    --noconfirm --clean --log-level WARN `
    --onefile --windowed `
    --name $portableName `
    --icon cove_icon.ico `
    --paths src `
    --add-data ("src\cove_pdf_editor\assets\cove_icon.png" + [IO.Path]::PathSeparator + "cove_pdf_editor\assets") `
    --collect-binaries pypdfium2_raw `
    --collect-binaries pymupdf `
    --exclude-module PySide6.QtWebEngineCore `
    --exclude-module PySide6.QtWebEngineWidgets `
    --exclude-module PySide6.QtQml `
    --exclude-module PySide6.QtQuick `
    --exclude-module PySide6.QtPdf `
    --exclude-module PySide6.Qt3DCore `
    --exclude-module PySide6.QtCharts `
    --exclude-module PySide6.QtDataVisualization `
    --exclude-module PySide6.QtMultimedia `
    --exclude-module PySide6.QtMultimediaWidgets `
    --exclude-module tkinter `
    packaging\launcher.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Step "[4/4] Moving to release"
New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null
$src  = Join-Path 'dist' "$portableName.exe"
$dest = Join-Path $ReleaseDir "$portableName.exe"
if (Test-Path $dest) { Remove-Item -Force $dest }
Copy-Item $src $dest -Force

Remove-Item cove_icon.ico -Force -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force .buildenv, build, dist -ErrorAction SilentlyContinue

Step "Done."
Write-Host "  Portable: $dest"
