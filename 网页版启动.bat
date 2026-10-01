@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================
echo   CFBox Maintain Panel (Web UI)
echo   Opening http://127.0.0.1:8899 ...
echo   Keep this window open while using it.
echo ==========================================
start "" http://127.0.0.1:8899
python webui.py
pause >nul
