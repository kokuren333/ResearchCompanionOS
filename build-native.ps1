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
    $rapidOcrPackage = (python -c "import rapidocr_onnxruntime, pathlib; print(pathlib.Path(rapidocr_onnxruntime.__file__).parent)").Trim()
    $rapidOcrModels = Join-Path $rapidOcrPackage "models"
    python -m PyInstaller --noconfirm --clean --onefile --noconsole --name research-companion-backend --hidden-import rapidocr_onnxruntime --hidden-import fitz --collect-all fitz --add-data "$rapidOcrModels;rapidocr_onnxruntime/models" --exclude-module rapidocr.inference_engine.tensorrt --exclude-module torch --exclude-module tensorflow --exclude-module pandas --exclude-module scipy --exclude-module matplotlib --exclude-module sklearn --exclude-module jupyter --distpath $backendDir --workpath $backendWork --specpath $nativeRoot (Join-Path $repoRoot "app.py")
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
