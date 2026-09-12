@echo off
setlocal
cd /d "%~dp0"

set "PREDICTED_TIMESTAMP="
cls

:wait_for_window
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$now=[DateTimeOffset]::UtcNow;$nowEpoch=$now.ToUnixTimeSeconds();$nextEpoch=([Math]::Floor($nowEpoch/900)+1)*900;$target=[DateTimeOffset]::FromUnixTimeSeconds([Int64]$nextEpoch);$label='Next UTC window';$predicted=[Int64]0;if([Int64]::TryParse($env:PREDICTED_TIMESTAMP,[ref]$predicted)-and $predicted -gt $nowEpoch){$target=[DateTimeOffset]::FromUnixTimeSeconds($predicted);$label='Predicted change window'};$cr=[char]13;while (($remaining=$target-[DateTimeOffset]::UtcNow).TotalMilliseconds -gt 0){$seconds=[Math]::Max(0,[Math]::Ceiling($remaining.TotalSeconds));$hours=[Math]::Floor($seconds/3600);$minutes=[Math]::Floor(($seconds-($hours*3600))/60);$secondsLeft=$seconds-($hours*3600)-($minutes*60);$text=('{0}: {1:yyyy-MM-dd HH:mm:ss} UTC | {2:00}:{3:00}:{4:00} remaining' -f $label,$target.UtcDateTime,$hours,$minutes,$secondsLeft);Write-Host -NoNewline ($cr+$text+'    ');Start-Sleep -Milliseconds 200};Write-Host ($cr+('{0} reached: {1:yyyy-MM-dd HH:mm:ss} UTC' -f $label,$target.UtcDateTime)+'                              ')"

if errorlevel 1 (
    echo Countdown failed.
    exit /b 1
)

timeout /t 3 /nobreak >nul
cls

echo.
echo Running stocks_cacher.exe --live...
echo.
".\stocks_cacher.exe" --live
if errorlevel 1 echo stocks_cacher.exe exited with an error.

echo.
echo Running stocks_algorithm_predictor.exe...
echo.
set "PREDICTION_OUTPUT=%TEMP%\stocks_prediction_%RANDOM%_%RANDOM%.json"
".\stocks_algorithm_predictor.exe" > "%PREDICTION_OUTPUT%"
set "PREDICTOR_EXIT=%ERRORLEVEL%"
type "%PREDICTION_OUTPUT%"

set "PREDICTED_TIMESTAMP="
if "%PREDICTOR_EXIT%"=="0" (
    for /f "delims=" %%T in ('powershell.exe -NoLogo -NoProfile -Command "$payload=ConvertFrom-Json (Get-Content -Raw -LiteralPath $env:PREDICTION_OUTPUT);if($null -eq $payload.predicted_change_timestamp){exit 1};[Console]::Write([Int64]$payload.predicted_change_timestamp)"') do set "PREDICTED_TIMESTAMP=%%T"
) else (
    echo stocks_algorithm_predictor.exe exited with an error.
)
del /q "%PREDICTION_OUTPUT%" >nul 2>&1

if not defined PREDICTED_TIMESTAMP (
    echo.
    echo Could not read predicted_change_timestamp; using the next UTC 15-minute window.
)

goto wait_for_window
