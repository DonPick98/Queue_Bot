@echo off
setlocal
cd /d "%~dp0"

echo Disattivo avvio automatico visibile per Mouth Queue Bot...
powershell -NoProfile -ExecutionPolicy Bypass -File ".\uninstall_startup_shortcut.ps1"
if errorlevel 1 goto error

echo.
echo Fatto. Il bot non partira piu automaticamente al login.
echo Se e gia aperto, chiudi la finestra del bot o premi CTRL+C per fermarlo.
pause
exit /b 0

:error
echo.
echo Errore durante la disattivazione dell'avvio automatico.
pause
exit /b 1
