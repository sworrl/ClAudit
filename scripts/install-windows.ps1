# Install ClAudit on Windows: a Start Menu shortcut and (optionally) a Startup entry so the tray
# app launches at login, minimized. Uses pythonw so no console window stays open.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1              # Start Menu only
#   powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1 -Autostart   # also run at login
#   powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1 -Uninstall
param(
    [switch]$Autostart,
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
Write-Host "Done. Launch 'ClAudit' from the Start Menu (notify-only by default; add --auto to auto-file)."
