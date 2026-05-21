$ErrorActionPreference = "Stop"

$StartupDir = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $StartupDir "Mouth Queue Bot.lnk"

if (Test-Path -LiteralPath $ShortcutPath) {
    Remove-Item -LiteralPath $ShortcutPath -Force
    Write-Host "Removed startup shortcut:"
    Write-Host $ShortcutPath
} else {
    Write-Host "Startup shortcut not found:"
    Write-Host $ShortcutPath
}
