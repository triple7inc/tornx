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

python -c "import chalk" >nul 2>&1
if errorlevel 1 (
    echo Chalk is not installed. Installing it now...
    python -m pip install chalk
    if errorlevel 1 (
        echo Failed to install Chalk.
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

echo Building stocks_predictor.exe...
python -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --name stocks_predictor ^
    --distpath dist ^
    --workpath build ^
    --specpath build ^
    stocks_predictor.py

if errorlevel 1 (
    echo Predictor build failed.
    exit /b 1
)

echo Building companion stocks_cacher.exe...
python -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --name stocks_cacher ^
    --distpath dist ^
    --workpath build ^
    --specpath build ^
    stocks_cacher.py

if errorlevel 1 (
    echo Cacher build failed.
    exit /b 1
)

if not exist "dist\stocks_cache.json" (
    if exist "stocks_cache.json" (
        copy /y "stocks_cache.json" "dist\stocks_cache.json" >nul
    )
)

echo Build complete: %~dp0dist\stocks_predictor.exe
echo stocks_cacher.exe and the mutable stocks_cache.json are beside the predictor.
exit /b 0
