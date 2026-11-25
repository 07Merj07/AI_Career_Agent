@echo off
REM AI Career Agent - one-time setup (Windows)
REM Double-click this file, or run it from a terminal in the project folder.
chcp 65001 >nul
cd /d "%~dp0"

echo === 1/5 Checking Python ===
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.11+ from python.org and tick "Add python.exe to PATH".
  pause
  exit /b 1
)
python --version

echo === 2/5 Creating the virtual environment (.venv) ===
if not exist ".venv\Scripts\python.exe" python -m venv .venv

echo === 3/5 Installing packages ===
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Package installation failed. Check your internet connection and run setup.bat again.
  pause
  exit /b 1
)

echo === 4/5 Creating your personal files from the examples ===
if not exist ".env" copy ".env.example" ".env" >nul && echo   created .env  (add your keys!)
if not exist "settings.json" copy "settings.example.json" "settings.json" >nul && echo   created settings.json
if not exist "CV\cv_content.json" copy "CV\cv_content.example.json" "CV\cv_content.json" >nul && echo   created CV\cv_content.json  (put your CV in it!)
for %%d in (Database Logs Generated_CVs) do if not exist "%%d" mkdir "%%d"

echo === 5/5 Local AI model ===
where ollama >nul 2>nul
if errorlevel 1 (
  echo Ollama is not installed. Get it from https://ollama.com and then run:  ollama pull qwen3.5:4b
) else (
  ollama pull qwen3.5:4b
)

echo.
echo Setup finished. Next steps (see README.md):
echo   1. Fill in .env (API key + Telegram token) and CV\cv_content.json
echo   2. Run:  .venv\Scripts\python.exe Python\setup_check.py --telegram
echo   3. Run:  powershell -ExecutionPolicy Bypass -File schedule_tasks.ps1
pause
