$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Dist = Join-Path $Root "dist"
$ZipPath = Join-Path $Dist "mouth-queue-justrunmy.zip"
$Temp = Join-Path $env:TEMP ("mouth-queue-justrunmy-" + [guid]::NewGuid().ToString("N"))

New-Item -ItemType Directory -Force -Path $Dist | Out-Null
New-Item -ItemType Directory -Force -Path $Temp | Out-Null

try {
    $items = @(
        "bot.py",
        "requirements.txt",
        "pyproject.toml",
        "Dockerfile",
        ".dockerignore",
        ".env.justrunmy.example",
        "JUSTRUNMY_APP.md",
        "src"
    )

    foreach ($item in $items) {
        $source = Join-Path $Root $item
        $destination = Join-Path $Temp $item
        if (Test-Path -LiteralPath $source -PathType Container) {
            Copy-Item -LiteralPath $source -Destination $destination -Recurse
        } else {
            Copy-Item -LiteralPath $source -Destination $destination
        }
    }

    Get-ChildItem -Path $Temp -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
    Get-ChildItem -Path $Temp -Recurse -File -Include "*.pyc", "*.pyo" | Remove-Item -Force

    if (Test-Path -LiteralPath $ZipPath) {
        Remove-Item -LiteralPath $ZipPath -Force
    }

    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::Open($ZipPath, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        $basePath = (Resolve-Path -LiteralPath $Temp).Path.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
        Get-ChildItem -LiteralPath $Temp -Recurse -File | ForEach-Object {
            $relative = $_.FullName.Substring($basePath.Length)
            $entryName = $relative.Replace([System.IO.Path]::DirectorySeparatorChar, "/")
            $entryName = $entryName.Replace([System.IO.Path]::AltDirectorySeparatorChar, "/")
            $entryName = $entryName.TrimStart("/")
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $zip,
                $_.FullName,
                $entryName,
                [System.IO.Compression.CompressionLevel]::Optimal
            ) | Out-Null
        }
    } finally {
        $zip.Dispose()
    }

    Write-Host "Created JustRunMy.App deployment zip:"
    Write-Host $ZipPath
} finally {
    if (Test-Path -LiteralPath $Temp) {
        Remove-Item -LiteralPath $Temp -Recurse -Force
    }
}
