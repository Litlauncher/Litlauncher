@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw LitLauncher.py
    exit /b
)
where python >nul 2>nul
if %errorlevel%==0 (
    python LitLauncher.py
    exit /b
)
echo Python не найден. Установи его с https://www.python.org/downloads/
echo При установке отметь галочку "Add python.exe to PATH".
pause
