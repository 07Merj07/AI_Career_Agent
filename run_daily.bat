@echo off
REM AI Career Agent - daily run (started by Windows Task Scheduler)
REM This file must be in the AI_Career_Agent main folder (next to .venv and Python).

chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"

if not exist "Logs" mkdir "Logs"

echo. >> "Logs\daily_run.log"
echo ===== %date% %time% ===== >> "Logs\daily_run.log"
".venv\Scripts\python.exe" "Python\daily_pipeline.py" >> "Logs\daily_run.log" 2>&1
echo ===== finished with code %errorlevel% ===== >> "Logs\daily_run.log"
