$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$cargoBin = Join-Path $env:USERPROFILE ".cargo\bin"
$env:Path = "$cargoBin;$env:Path"

Push-Location (Join-Path $repoRoot "native")
try {
    if (-not (Test-Path "node_modules")) {
        npm install
    }
    npm run tauri -- dev
}
finally {
    Pop-Location
}
