param(
  [string]$OutPath = "./scripts/himalaya-account-main-proxy.cmd",
  [string]$HimalayaExe = "C:/Users/dagobert-ai/scoop/shims/himalaya.exe"
)

$ErrorActionPreference = "Stop"

$outAbs = if ([System.IO.Path]::IsPathRooted($OutPath)) {
  $OutPath
} else {
  Join-Path (Get-Location) $OutPath
}

$dir = Split-Path $outAbs -Parent
if (!(Test-Path $dir)) {
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

$himalayaWin = $HimalayaExe.Replace("/", "\\")

$cmd = @"
@echo off
setlocal
set "HIMALAYA_EXE=$himalayaWin"

if /I "%~1"=="--trace" (
  shift
  "%HIMALAYA_EXE%" --trace %*
) else (
  "%HIMALAYA_EXE%" %*
)
"@

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($outAbs, $cmd, $utf8NoBom)

Write-Host "Wrapper written: $outAbs"
