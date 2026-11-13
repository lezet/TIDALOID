# Builds dist\TIDALOID.exe in a separate build environment (.venv-build).
#   powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .venv-build)) {
    py -3.12 -m venv .venv-build
}
& .venv-build\Scripts\python.exe -m pip install --quiet --upgrade pip
& .venv-build\Scripts\python.exe -m pip install --quiet -r requirements.txt -r requirements-build.txt
& .venv-build\Scripts\pyinstaller.exe --noconfirm --clean tidaloid.spec

$exe = Get-Item dist\TIDALOID.exe
"Built $($exe.FullName)  ($([math]::Round($exe.Length / 1MB, 1)) MB)"
