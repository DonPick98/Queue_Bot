@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
    echo Uso: restore_state.bat percorso\backup.zip
    echo Esempio: restore_state.bat state_backups\mouth-queue-state-20260521-120000.zip
    pause
    exit /b 1
)

echo IMPORTANTE: ferma il bot prima di ripristinare lo stato.
echo.
".venv\Scripts\python.exe" scripts\restore_state.py "%~1"
if errorlevel 1 goto error

echo.
echo Restore completato.
pause
exit /b 0

:error
echo.
echo Errore durante il restore.
pause
exit /b 1
