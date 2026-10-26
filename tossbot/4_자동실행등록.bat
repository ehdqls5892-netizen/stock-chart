@echo off
chcp 65001 >nul
cd /d %~dp0
set MODE=paper
set /p MODE=모드를 입력하세요 (paper=연습, live=실전) [paper]: 
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\register_task.ps1" -Mode %MODE%
pause
