$ErrorActionPreference = "Stop"
$env:PYTHONUNBUFFERED = "1"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

$LogDir = Join-Path $Root "logs"
$LogPath = Join-Path $LogDir "bot.log"
$Python = Join-Path $Root ".venv\Scripts\python.exe"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-BotLog {
    param([string]$Message)
    $line = "$(Get-Date -Format o) $Message"
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
}

Write-BotLog "=== Scheduled bot startup ==="

if (-not (Test-Path -LiteralPath (Join-Path $Root ".env"))) {
    Write-BotLog "ERROR: .env not found."
    exit 1
}

if (-not (Test-Path -LiteralPath $Python)) {
    Write-BotLog "Creating virtual environment."
    & python -m venv ".venv" *>> $LogPath
    if ($LASTEXITCODE -ne 0) {
        Write-BotLog "ERROR: virtual environment creation failed with exit code $LASTEXITCODE."
        exit $LASTEXITCODE
    }
}

& $Python -c "import telegram, dotenv, apscheduler" *> $null
if ($LASTEXITCODE -ne 0) {
    Write-BotLog "Installing dependencies."
    & $Python -m pip install -r "requirements.txt" *>> $LogPath
    if ($LASTEXITCODE -ne 0) {
        Write-BotLog "ERROR: dependency installation failed with exit code $LASTEXITCODE."
        exit $LASTEXITCODE
    }
}

Write-BotLog "Starting bot.py."
& $Python "bot.py" *>> $LogPath
$exitCode = $LASTEXITCODE
Write-BotLog "bot.py exited with code $exitCode."
exit $exitCode
