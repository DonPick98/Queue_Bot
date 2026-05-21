@echo off
setlocal
cd /d "%~dp0"

echo Creo backup portabile dello stato del bot...
".venv\Scripts\python.exe" scripts\backup_state.py
if errorlevel 1 goto error

echo.
echo Backup completato. Trovi il file in state_backups.
pause
exit /b 0

:error
echo.
echo Errore durante il backup.
pause
exit /b 1
