@echo off
chcp 65001 >nul
cd /d %~dp0
echo 장이 열려 있을 때만 실행하세요. 1주를 사고 손절 조건주문을 걸었다 지우고 다시 팝니다.
echo 지금 열린 시장(국장/미장)을 알아서 골라요. 미장은 달러 또는 자동 환전이 필요해요.
pause
.venv\Scripts\python -m bot.selftest
pause
