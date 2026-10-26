@echo off
chcp 65001 >nul
cd /d %~dp0
chcp 65001 >nul
.venv\Scripts\python -m bot.main check
pause
