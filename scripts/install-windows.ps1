# Install ClAudit on Windows: a Start Menu shortcut and (optionally) a Startup entry so the tray
# app launches at login, minimized. Uses pythonw so no console window stays open.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1              # Start Menu only
#   powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1 -Autostart   # also run at login
#   powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1 -NoCensus     # heartbeat OFF
#   powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1 -Uninstall
param(
    [switch]$Autostart,
    [switch]$NoCensus,
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"

$Dir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Programs = [Environment]::GetFolderPath("Programs")
$Startup  = [Environment]::GetFolderPath("Startup")
$MenuLnk  = Join-Path $Programs "ClAudit.lnk"
$StartLnk = Join-Path $Startup  "ClAudit.lnk"

if ($Uninstall) {
    Remove-Item -ErrorAction SilentlyContinue $MenuLnk, $StartLnk
    Write-Host "Removed $MenuLnk and $StartLnk"
    exit 0
}

$Pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $Pyw) { $Pyw = (Get-Command python.exe).Source }   # falls back to a console python

function New-ClauditShortcut($Path, $Args) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($Path)
    $lnk.TargetPath = $Pyw
    $lnk.Arguments = "`"$Dir\claudit_gui.py`" $Args"
    $lnk.WorkingDirectory = $Dir
    $lnk.Description = "Watch Claude Code for false-positive safety/AUP blocks"
    $ico = Join-Path $Dir "claudit_icon.ico"
    if (Test-Path $ico) { $lnk.IconLocation = $ico }
    $lnk.Save()
}

New-ClauditShortcut $MenuLnk "--interval 30"
Write-Host "Installed Start Menu shortcut -> $MenuLnk"
if ($Autostart) {
    New-ClauditShortcut $StartLnk "--interval 30 --hidden"
    Write-Host "Enabled autostart            -> $StartLnk"
}
if ($NoCensus) {
    $cfgDir = Join-Path $HOME ".claude\claudit"
    New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
    $cfgPath = Join-Path $cfgDir "config.json"
    $cfg = @{}
    if (Test-Path $cfgPath) { try { $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json -AsHashtable } catch { $cfg = @{} } }
    $cfg["census_anon"] = $false
    $cfg["census_notice_shown"] = $true
    $cfg | ConvertTo-Json | Set-Content -Path $cfgPath -Encoding UTF8
    Write-Host "Anonymous install heartbeat: OFF (saved to $cfgPath)."
} else {
    Write-Host "CENSUS: ClAudit sends an anonymous heartbeat every 10 minutes while it runs: a random node id," -ForegroundColor Red
    Write-Host "the version, the OS family, and git-or-pip, to a Cloudflare Worker the maintainer runs. No IP," -ForegroundColor Red
    Write-Host "hostname, account, or content is kept. It is ON by default." -ForegroundColor Red
    Write-Host "Opt out: re-run with -NoCensus, or Settings > Census in the app, or CLAUDIT_NO_CENSUS=1." -ForegroundColor Red
}
Write-Host "Done. Launch 'ClAudit' from the Start Menu (notify-only by default; add --auto to auto-file)."
