@echo off
setlocal

set "SHORTCUT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Mouth Queue Bot.lnk"

if exist "%SHORTCUT%" (
    echo Avvio automatico: ATTIVO
    echo Collegamento:
    echo %SHORTCUT%
) else (
    echo Avvio automatico: DISATTIVO
)

echo.
pause
