# 오라클 클라우드 서버에서 돌리기

실시간 모의매매(`run.py live`)를 서버에서 24시간 돌리는 방법입니다. 서버가 재부팅돼도 자동으로 다시 시작되고,
오류로 꺼져도 30초 뒤 다시 켜집니다. 대시보드는 서버 안에서만 열리고, 내 PC 에서 SSH 터널로 봅니다.

## 0. 서버 확인 (한 번만)

서버에 SSH 로 접속한 뒤 아래를 실행해 결과를 확인합니다.

```bash
cat /etc/os-release | head -3; uname -m; python3 --version; free -h | head -2; df -h / | tail -1
```

- 파이썬 3.10 이상이 필요합니다 (설치 스크립트가 자동으로 찾거나 설치합니다).
- 메모리가 1GB 이하인 작은 서버라면 설치 중 메모리가 부족할 수 있습니다. 그때는 스왑을 먼저 만드세요:
  `sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile`

## 1. 설치

```bash
git clone https://github.com/baekitae/bit ~/bit
bash ~/bit/deploy/install.sh
```

코드 받기, 파이썬 가상환경, 라이브러리, 시세 데이터, 자동 실행 등록까지 합니다. (몇 분)

## 2. 키 넣기

```bash
nano ~/bit/.env
```

```
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...            # G 트랙 (없으면 G 만 skip)
TELEGRAM_BOT_TOKEN=...        # 알림 (없으면 알림 꺼짐)
TELEGRAM_CHAT_ID=...
```

- nano 저장: `Ctrl+O` → `Enter`, 나가기: `Ctrl+X`
- 텔레그램은 지금 쓰고 있는 봇의 토큰을 그대로 써도 되고, @BotFather 에서 새 봇을 만들어도 됩니다.
  메시지를 보내기만 하므로 기존 봇 프로그램과 충돌하지 않습니다.
- `.env` 는 본인만 읽을 수 있게 권한이 설정됩니다 (600). GitHub 에는 올라가지 않습니다.

## 3. 시험

```bash
cd ~/bit
.venv/bin/python run.py council-test --track B1     # Claude 위원회 1회 (약 230원)
.venv/bin/python run.py notify-test                 # 텔레그램에 시험 메시지
```

## 4. 시작 (이 순간이 6개월 비교 시작일)

```bash
sudo systemctl enable --now bit-live
journalctl -u bit-live -f          # 실시간 로그 (나가기: Ctrl+C, 봇은 계속 돎)
```

| 하고 싶은 것 | 명령 |
|---|---|
| 상태 보기 | `systemctl status bit-live` |
| 멈추기 | `sudo systemctl stop bit-live` |
| 다시 켜기 | `sudo systemctl restart bit-live` |
| 최근 로그 100줄 | `journalctl -u bit-live -n 100` |
| C 트랙 버전 이력 | `cd ~/bit && .venv/bin/python run.py evolution` |

## 5. 대시보드 보기 (내 PC 에서)

Windows PowerShell 에서 (서버 접속할 때 쓰는 키 파일과 주소로 바꿔서):

```
ssh -i C:\경로\키파일.key -L 8501:127.0.0.1:8501 ubuntu@서버IP
```

이 창을 켜 둔 채로 브라우저에서 http://localhost:8501 을 엽니다.
대시보드 포트를 인터넷에 직접 열지 마세요 (누구나 볼 수 있게 됩니다).

## 6. 업데이트 (코드가 바뀌었을 때)

```bash
bash ~/bit/deploy/install.sh
sudo systemctl restart bit-live bit-dashboard
```

상태는 DB 에 저장되어 있어 재시작해도 이어집니다. 재시작 동안의 봉은 소급 매매하지 않습니다.

## 7. 백업

- 매일 자동으로 `~/bit/data/backups/` 에 DB 백업이 생깁니다 (최근 7개).
- PC 로 가져오기 (PowerShell): `scp -i C:\경로\키파일.key ubuntu@서버IP:~/bit/data/backups/*.db .`
- 수동 백업: `cd ~/bit && .venv/bin/python run.py backup`

## 주의

- **PC 와 서버에서 동시에 `live` 를 돌리지 마세요.** 각각 다른 기록이 쌓여 헷갈립니다. 서버로 정했으면 서버만.
- 오라클 무료 서버는 CPU 를 거의 안 쓰면 회수될 수 있다는 정책이 있습니다. 계정 조건을 확인하세요.
