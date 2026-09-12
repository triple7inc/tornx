@echo off
setlocal
cd /d "%~dp0"

echo Checking build dependencies...
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller is not installed. Installing it now...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo Failed to install PyInstaller.
        exit /b 1
    )
)

echo Building stocks_server.exe...
python -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --name stocks_server ^
    --distpath dist ^
    --workpath build\stocks_server ^
    --specpath build ^
    stocks_server.py

if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

echo Build complete: %~dp0dist\stocks_server.exe
echo The executable serves files beside itself on port 8676.
echo Its first launch requests one-time Windows Firewall approval.
exit /b 0
