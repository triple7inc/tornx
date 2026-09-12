@echo off
setlocal
cd /d "%~dp0"

echo Checking PyInstaller...
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller is not installed. Installing it now...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo Failed to install PyInstaller.
        exit /b 1
    )
)

echo Building stocks_cacher.exe...
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
    echo Build failed.
    exit /b 1
)

echo Build complete: %~dp0dist\stocks_cacher.exe
echo stocks_cache.json will be created beside the executable.
exit /b 0
