$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$releaseRoot = Join-Path $repoRoot "native\src-tauri\target\release"
$exe = Join-Path $releaseRoot "research-companion-native.exe"
$dist = Join-Path $repoRoot "dist"
$version = ((Get-Content (Join-Path $repoRoot "native\src-tauri\tauri.conf.json") -Raw | ConvertFrom-Json).version)
$archive = Join-Path $dist "ResearchCompanion-$version-windows-x64.zip"
$stage = Join-Path ([IO.Path]::GetTempPath()) ("ResearchCompanion-release-" + [Guid]::NewGuid().ToString("N"))

if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "Release executable not found. Run .\build-native.ps1 first: $exe"
}

New-Item -ItemType Directory -Force -Path $dist, $stage | Out-Null
try {
    Copy-Item -LiteralPath $exe -Destination (Join-Path $stage "Research Companion.exe")
    Copy-Item -LiteralPath (Join-Path $repoRoot "README.md"), (Join-Path $repoRoot "README.en.md"), (Join-Path $repoRoot "README.ja.md"), (Join-Path $repoRoot "THIRD_PARTY_NOTICES.md") -Destination $stage
    Copy-Item -LiteralPath (Join-Path $repoRoot "licenses") -Destination $stage -Recurse
    if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }
    Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $archive -CompressionLevel Optimal
} finally {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
}

Write-Output "Created $archive"
