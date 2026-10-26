@echo off
chcp 65001 >nul
cd /d %~dp0
echo [1/3] 파이썬 가상환경 만들기...
py -3 -m venv .venv || python -m venv .venv
echo [2/3] 필요한 도구 설치...
.venv\Scripts\python -m pip install --upgrade pip >nul
.venv\Scripts\pip install -r requirements.txt
if not exist .env copy .env.example .env >nul
echo [3/3] 메모장이 열리면 토스 Client Id / Secret 를 붙여넣고 저장하세요.
notepad .env
echo 설치 끝. 이제 1_연결확인.bat 을 실행하세요.
pause
