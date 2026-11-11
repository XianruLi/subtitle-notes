@echo off
cd /d "%~dp0"
if not exist ".env" (
  echo [!] .env not found.
  echo     Copy .env.example to .env and put your DEEPSEEK_API_KEY inside.
  pause
  exit /b 1
)
python -m src.srt_daemon.app %*
if errorlevel 1 pause
