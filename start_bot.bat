@echo off
setlocal
cd /d "%~dp0"

echo Avvio Telegram Channel Scheduler Bot...
echo.

if not exist ".env" (
    echo ERRORE: file .env non trovato.
    echo Copia .env.example in .env e compila token, admin e canale.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creo ambiente virtuale Python...
    python -m venv .venv
    if errorlevel 1 goto error
)

echo Installo/aggiorno dipendenze...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto error

echo.
echo Bot in avvio. Lascia questa finestra aperta.
echo Per fermarlo premi CTRL+C.
echo.
".venv\Scripts\python.exe" bot.py
if errorlevel 1 goto error

echo.
echo Il bot si e fermato.
pause
exit /b 0

:error
echo.
echo Si e verificato un errore. Copia le righe qui sopra se vuoi che lo analizzi.
pause
exit /b 1
