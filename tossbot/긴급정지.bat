@echo off
chcp 65001 >nul
cd /d %~dp0
echo stop> STOP
echo 긴급 정지 파일을 만들었어요. 로봇이 새로 사지 않고, 보유 중인 건 바로 팝니다.
echo 다시 매매하려면 재개.bat 을 실행하세요.
pause
