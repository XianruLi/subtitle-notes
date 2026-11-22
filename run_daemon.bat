@echo off
rem Start the notes daemon (watches the folder configured in config.yaml).
cd /d "%~dp0"
python -m src.srt_daemon.main %*
if errorlevel 1 pause
