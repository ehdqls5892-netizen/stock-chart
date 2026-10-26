@echo off
chcp 65001 >nul
cd /d %~dp0
del STOP 2>nul
echo 긴급 정지를 풀었어요.
pause
