@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

if not exist logs mkdir logs
if not exist data mkdir data

if not exist ".venv\Scripts\activate.bat" (
    echo [ERRO] Ambiente virtual nao encontrado em .venv
    echo Crie o ambiente com:
    echo   python -m venv .venv
    echo   .venv\Scripts\activate
    echo   pip install -r requirements.txt
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"
set PYTHON_EXE=.venv\Scripts\python.exe

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i

set SCANNER_LOG_FILE=logs\token_scanner_%TODAY%.txt
set RUNNER_LOG_FILE=logs\krptov_runner_%TODAY%.txt
set CYCLE_INTERVAL_SECONDS=60

for /f %%i in ('powershell -NoProfile -Command "$yaml = Get-Content config\config.yaml; $line = $yaml | Where-Object { $_ -match '^\s*cycle_interval_seconds\s*:' } | Select-Object -First 1; if ($line) { ($line -split ':', 2)[1].Trim() }"') do set CYCLE_INTERVAL_SECONDS=%%i

echo ===============================================
echo KRPTO-V Scanner + Social Inference
echo Log scanner: %SCANNER_LOG_FILE%
echo Log batch: %RUNNER_LOG_FILE%
echo Intervalo: %CYCLE_INTERVAL_SECONDS% segundos
echo Para parar: CTRL+C
echo ===============================================

:loop

for /f "tokens=1-2 delims= " %%a in ('powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss'"') do (
    set NOW_DATE=%%a
    set NOW_TIME=%%b
)

echo.
echo ===============================
echo !NOW_DATE! !NOW_TIME!
echo Rodando ciclo do scanner e inferencia social...

echo. >> "%RUNNER_LOG_FILE%"
echo =============================== >> "%RUNNER_LOG_FILE%"
echo !NOW_DATE! !NOW_TIME! >> "%RUNNER_LOG_FILE%"

"%PYTHON_EXE%" app.py >> "%RUNNER_LOG_FILE%" 2>&1
set EXIT_CODE=!ERRORLEVEL!

if not "!EXIT_CODE!"=="0" (
    echo [ERRO] Ciclo falhou. Veja o log: %RUNNER_LOG_FILE%
    echo [ERRO] Exit code: !EXIT_CODE! >> "%RUNNER_LOG_FILE%"
) else (
    echo Ciclo concluido. Veja os logs em logs\
)

echo Aguardando %CYCLE_INTERVAL_SECONDS% segundos...
powershell -NoProfile -Command "Start-Sleep -Seconds %CYCLE_INTERVAL_SECONDS%"

goto loop
