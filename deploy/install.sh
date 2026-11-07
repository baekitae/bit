#!/usr/bin/env bash
# BTC 위원회 봇 서버 설치 (Ubuntu/Debian 또는 Oracle Linux). 여러 번 실행해도 안전하다 (업데이트에도 사용).
#   bash deploy/install.sh
set -euo pipefail

REPO="https://github.com/baekitae/bit"
DIR="${BIT_DIR:-$HOME/bit}"
USER_NAME="$(id -un)"

echo "== 1/6 시스템 패키지"
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -y -qq
  sudo apt-get install -y -qq git python3 python3-venv python3-pip sqlite3
elif command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y -q git sqlite || true
  sudo dnf install -y -q python3.12 python3.12-pip || sudo dnf install -y -q python3.11 python3.11-pip || sudo dnf install -y -q python3 python3-pip
fi

echo "== 2/6 파이썬 3.10 이상 찾기"
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then echo "파이썬 3.10 이상이 필요합니다. 서버 OS 를 알려 주세요."; exit 1; fi
echo "   사용: $PY ($($PY --version))"

echo "== 3/6 코드 받기 ($DIR)"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only; else git clone -q "$REPO" "$DIR"; fi
cd "$DIR"

echo "== 4/6 가상환경과 라이브러리 (몇 분 걸릴 수 있음)"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "== 5/6 설정 파일과 시세 데이터"
if [ ! -f .env ]; then cp .env.example .env; echo "   .env 를 새로 만들었습니다 → 키를 넣으세요: nano $DIR/.env"; fi
chmod 600 .env
.venv/bin/python run.py fetch | tail -2

echo "== 6/6 자동 실행 등록 (systemd)"
sudo tee /etc/systemd/system/bit-live.service >/dev/null <<UNIT
[Unit]
Description=BTC council paper trading (live loop)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$DIR/.venv/bin/python run.py live
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT
sudo tee /etc/systemd/system/bit-dashboard.service >/dev/null <<UNIT
[Unit]
Description=BTC council dashboard (localhost only)
After=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/streamlit run src/report/dashboard.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now bit-dashboard >/dev/null 2>&1 || true

echo
echo "설치 완료. 다음 단계:"
echo "  1) 키 넣기:        nano $DIR/.env"
echo "  2) AI 시험:        cd $DIR && .venv/bin/python run.py council-test --track B1"
echo "  3) 알림 시험:      .venv/bin/python run.py notify-test"
echo "  4) 실시간 시작:    sudo systemctl enable --now bit-live"
echo "  5) 로그 보기:      journalctl -u bit-live -f"
if systemctl is-active --quiet bit-live; then
  echo
  echo "bit-live 가 이미 실행 중입니다. 업데이트를 반영하려면: sudo systemctl restart bit-live bit-dashboard"
fi
