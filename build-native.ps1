$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$cargoBin = Join-Path $env:USERPROFILE ".cargo\bin"
$env:Path = "$cargoBin;$env:Path"
$nativeRoot = Join-Path $repoRoot "native"
$backendDir = Join-Path $nativeRoot "src-tauri\backend"
$backendWork = Join-Path $nativeRoot "pyinstaller-work"
New-Item -ItemType Directory -Force -Path $backendDir | Out-Null

Push-Location $nativeRoot
try {
    if (-not (Test-Path "node_modules")) {
        npm install
    }
    python -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        python -m pip install pyinstaller
    }
    python -m pip install -r (Join-Path $repoRoot "requirements.txt")
    python -m PyInstaller --noconfirm --clean --onefile --noconsole --name research-companion-backend --distpath $backendDir --workpath $backendWork --specpath $nativeRoot (Join-Path $repoRoot "app.py")
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
    npm run tauri -- build
    if ($LASTEXITCODE -ne 0) {
        throw "Tauri build failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
