@echo off
rem Build the Agent Usage release binary (single windowed .exe).
rem Output: dist\AgentUsage.exe

setlocal
cd /d "%~dp0"

echo === Installing build dependencies ===
python -m pip install --upgrade pyinstaller -r requirements.txt || goto :error

echo.
echo === Cleaning previous build ===
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo === Building AgentUsage.exe ===
python -m PyInstaller --noconfirm AgentUsage.spec || goto :error

echo.
echo === Done ===
echo Binary is at: dist\AgentUsage.exe
echo Ship it alongside a .env file (see .env.example).
goto :eof

:error
echo.
echo BUILD FAILED (exit code %errorlevel%).
exit /b %errorlevel%
