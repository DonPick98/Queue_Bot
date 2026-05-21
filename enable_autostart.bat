@echo off
setlocal
cd /d "%~dp0"

echo Attivo avvio automatico visibile per Mouth Queue Bot...
powershell -NoProfile -ExecutionPolicy Bypass -File ".\install_startup_shortcut.ps1"
if errorlevel 1 goto error

echo.
echo Fatto. Il bot partira quando accedi a Windows.
pause
exit /b 0

:error
echo.
echo Errore durante l'attivazione dell'avvio automatico.
pause
exit /b 1
