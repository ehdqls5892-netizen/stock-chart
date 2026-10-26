@echo off
chcp 65001 >nul
cd /d %~dp0
title 토스 단타 로봇 - 이 창을 닫으면 로봇이 꺼져요
.venv\Scripts\python -m bot.app
pause
