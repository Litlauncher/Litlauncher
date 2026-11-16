@echo off
chcp 65001 >nul
cd /d "%~dp0"
title LitLauncher - сборка
echo.
echo === LitLauncher: сборка .exe и установщика Setup ===
echo.
where python >nul 2>nul
if errorlevel 1 goto nopython

echo [1/3] Установка PyInstaller...
python -m pip install --upgrade pyinstaller
if errorlevel 1 goto fail

echo.
echo [2/3] Сборка LitLauncher.exe...
python -m PyInstaller --noconfirm --onefile --noconsole --name LitLauncher --icon icon.ico LitLauncher.py
if errorlevel 1 goto fail

echo.
echo [3/3] Сборка установщика Setup...
call :findiscc
if defined ISCC goto compile
echo Inno Setup не найден, пробую установить через winget...
winget install -e --id JRSoftware.InnoSetup --silent --accept-package-agreements --accept-source-agreements
call :findiscc
if defined ISCC goto compile
goto noinno

:compile
"%ISCC%" installer.iss
if errorlevel 1 goto fail
echo.
echo ГОТОВО:
echo   dist\LitLauncher.exe          - портативная версия
echo   dist\LitLauncher-Setup.exe    - установщик
explorer dist
pause
exit /b 0

:findiscc
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
exit /b

:noinno
echo.
echo LitLauncher.exe собран: dist\LitLauncher.exe
echo Для Setup поставь Inno Setup: https://jrsoftware.org/isdl.php
echo После установки запусти build.bat ещё раз.
explorer dist
pause
exit /b 0

:nopython
echo Python не найден. Установи его с https://www.python.org/downloads/
echo При установке отметь галочку Add python.exe to PATH.
pause
exit /b 1

:fail
echo.
echo Сборка не удалась, смотри ошибки выше.
pause
exit /b 1
