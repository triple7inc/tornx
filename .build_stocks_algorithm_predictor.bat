@echo off
setlocal
cd /d "%~dp0"

echo Checking build dependencies...
python -c "import numpy" >nul 2>&1
if errorlevel 1 (
    echo NumPy is not installed. Installing it now...
    python -m pip install numpy
    if errorlevel 1 (
        echo Failed to install NumPy.
        exit /b 1
    )
)

python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller is not installed. Installing it now...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo Failed to install PyInstaller.
        exit /b 1
    )
)

echo Building stocks_algorithm_predictor.exe...
python -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --name stocks_algorithm_predictor ^
    --distpath dist ^
    --workpath build ^
    --specpath build ^
    stocks_algorithm_predictor.py

if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

if not exist "dist\stocks_cache.json" if exist "stocks_cache.json" (
    echo Copying the immutable base cache beside the executable...
    copy /y "stocks_cache.json" "dist\stocks_cache.json" >nul
)

if not exist "dist\stocks_cache.jsonl" if exist "stocks_cache.jsonl" (
    copy /y "stocks_cache.jsonl" "dist\stocks_cache.jsonl" >nul
)

if not exist "dist\stocks_forward_test.json" if exist "stocks_forward_test.json" (
    copy /y "stocks_forward_test.json" "dist\stocks_forward_test.json" >nul
)

echo Build complete: %~dp0dist\stocks_algorithm_predictor.exe
echo Keep stocks_cache.json, stocks_cache.jsonl, and stocks_forward_test.json beside it.
exit /b 0
