@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m src.srt_daemon.progress --watch 2
pause
