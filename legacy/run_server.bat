@echo off
echo [SYSTEM] Starting AI Hedge Fund Server...
cd /d "%~dp0"
if not exist .venv (
    echo [SYSTEM] Virtual environment not found. Please create one and install requirements.txt
    exit /b 1
)
call .venv\Scripts\activate.bat
python main.py >> server.log 2>&1
