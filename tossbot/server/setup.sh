#!/usr/bin/env bash
# 토스 단타 로봇 — 클라우드 서버(우분투) 설치. 사용법: bash server/setup.sh
set -e
cd "$(dirname "$0")/.."
DIR="$(pwd)"
echo "== 1/4 프로그램 설치 (1~2분)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip unzip nano ufw fail2ban unattended-upgrades >/dev/null
# 보안: 접속 문은 SSH(22) 하나만 열고, 비밀번호 무작위 대입은 자동 차단, 보안 업데이트 자동 설치
sudo ufw allow OpenSSH >/dev/null && sudo ufw --force enable >/dev/null
sudo systemctl enable --now fail2ban >/dev/null 2>&1 || true
sudo timedatectl set-timezone Asia/Seoul || true
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
echo "== 2/4 키 넣기"
if [ ! -f .env ]; then cp .env.example .env; fi
grep -q '^APP_HEADLESS=' .env || printf '\n# 서버용\nAPP_HEADLESS=1\nAPP_RESUME=1\n' >> .env
chmod 600 .env
echo "   메모장(nano)이 열려요. TOSS_CLIENT_ID, TOSS_CLIENT_SECRET, TG_TOKEN, TG_CHAT 을 넣고"
echo "   Ctrl+O → Enter (저장) → Ctrl+X (닫기) 하세요."
read -p "   준비되면 Enter… " _
nano .env
echo "== 3/4 연결 확인"
.venv/bin/python -m bot.main check || true
echo "== 4/4 자동 실행 등록 (서버가 켜지면 로봇 프로그램도 자동으로 켜짐)"
sudo tee /etc/systemd/system/tossbot.service >/dev/null <<UNIT
[Unit]
Description=Toss trading bot
After=network-online.target
Wants=network-online.target

[Service]
User=$(whoami)
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python -m bot.app
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now tossbot
sudo ln -sf "$DIR/server/tb" /usr/local/bin/tb
chmod +x "$DIR/server/tb"
"$DIR/server/tb" 자동업데이트 >/dev/null 2>&1 || true
echo
echo "✅ 설치 끝. 이제 텔레그램 봇 채팅방에서 '📊 상태' → '🔴 실전 시작' 을 누르세요."
echo "   서버 명령: tb 상태 | tb 기록 | tb 확인 | tb 시험 | tb 재시작 | tb 업데이트"
