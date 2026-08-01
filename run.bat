@echo off
chcp 65001 >nul
cd /d "%~dp0"
"C:\Users\bvdov\AppData\Local\Programs\Python\Python312\python.exe" main.py
echo.
echo === Bot stopped. Press any key to close this window. ===
pause >nul
