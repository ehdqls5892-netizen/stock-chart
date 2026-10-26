@echo off
cd /d %~dp0
chcp 65001 >nul
title 토스 단타 로봇 - 연습
.venv\Scripts\python -m bot.main paper
