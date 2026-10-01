@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================
echo   CFBox Maintain Script (APPLY mode)
echo   This will UPDATE qinyu config online.
echo ==========================================
python cfbox_maintain.py
echo.
echo Done. Press any key to close...
pause >nul
