@echo off
setlocal
cd /d "%~dp0"

echo Creo zip pulito per JustRunMy.App...
powershell -NoProfile -ExecutionPolicy Bypass -File ".\build_justrunmy_zip.ps1"
if errorlevel 1 goto error

echo.
echo Fatto. Puoi caricare lo zip dalla cartella dist.
pause
exit /b 0

:error
echo.
echo Errore durante la creazione dello zip.
pause
exit /b 1
