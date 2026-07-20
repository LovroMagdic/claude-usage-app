@echo off
rem Silent launcher for the Agent Usage tray app (pythonw = no console window).
start "" "%LocalAppData%\Programs\Python\Python311\pythonw.exe" "%~dp0tray_app.py"
