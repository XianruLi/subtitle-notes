# Build SRTDaemon.exe and assemble the portable folder.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File build_exe.ps1
#   (or: pwsh -File build_exe.ps1  when PowerShell 7 is installed)
#
# Result: dist\SRTDaemon\SRTDaemon.exe  (double-click to open the GUI)
#requires -Version 5
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

Write-Host '[1/3] Running PyInstaller...'
python -m PyInstaller --noconfirm --clean srt_daemon.spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }

$dist = Join-Path $root 'dist\SRTDaemon'
$exe = Join-Path $dist 'SRTDaemon.exe'
if (-not (Test-Path $exe)) { throw "Build output missing: $exe" }

Write-Host '[2/3] Assembling the portable folder...'
Copy-Item (Join-Path $root 'src\srt_daemon\config.yaml') (Join-Path $dist 'config.yaml') -Force
Copy-Item (Join-Path $root '.env.example') (Join-Path $dist '.env.example') -Force -ErrorAction SilentlyContinue
if (Test-Path (Join-Path $root '.env')) {
    Copy-Item (Join-Path $root '.env') (Join-Path $dist '.env') -Force
    Write-Host '      .env copied (contains your API key - do not share this folder)'
}
New-Item -ItemType Directory -Force -Path (Join-Path $dist 'doc') | Out-Null
Copy-Item (Join-Path $root 'doc\prompts') (Join-Path $dist 'doc') -Recurse -Force
if (Test-Path (Join-Path $root 'models')) {
    Copy-Item (Join-Path $root 'models') (Join-Path $dist 'models') -Recurse -Force
}
foreach ($folder in @('output', 'logs')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $dist $folder) | Out-Null
}
$guide = Join-Path $root 'doc\GUI.md'
if (Test-Path $guide) { Copy-Item $guide (Join-Path $dist '使用说明.md') -Force }

Write-Host '[3/3] Running the packaged self-test...'
Start-Process -FilePath $exe -ArgumentList '--selftest' -Wait -NoNewWindow
# The self-test log lands next to the data root: the EXE folder for a portable
# release, but the checkout root for a development build (dist/SRTDaemon/...).
$log = Join-Path $dist 'selftest.log'
if (-not (Test-Path $log)) { $log = Join-Path $root 'selftest.log' }
if (Test-Path $log) {
    Get-Content $log -Encoding UTF8
    if (Select-String -Path $log -Pattern 'SELFTEST OK' -Quiet) {
        Write-Host ''
        Write-Host "OK: $exe"
    } else {
        Write-Host ''
        Write-Warning 'Self-test reported problems (see above)'
    }
} else {
    Write-Warning 'selftest.log was not produced'
}
