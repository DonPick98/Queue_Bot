$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$StartupDir = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $StartupDir "Mouth Queue Bot.lnk"
$TargetPath = Join-Path $Root "start_bot.bat"

if (-not (Test-Path -LiteralPath $TargetPath)) {
    throw "Start script not found: $TargetPath"
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $TargetPath
$shortcut.WorkingDirectory = $Root
$shortcut.Description = "Start Mouth Queue Telegram Bot"
$shortcut.Save()

Write-Host "Installed visible startup shortcut:"
Write-Host $ShortcutPath
