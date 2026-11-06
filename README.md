# BTC AI 위원회 모의매매 봇

업비트 KRW-BTC에서 세 가지 방식(A: 규칙 전략, B: 규칙 + AI 필터, C: 규칙 + AI 필터 + 전략 진화)을
같은 조건에서 모의매매로 비교하는 프로젝트입니다. 설계는 [DESIGN.md](DESIGN.md)를 기준으로 합니다.

**현재: 4단계** (실시간 모의매매: A1~A5 규칙, B1~B4 Claude 위원회, G1~G4 Gemini 위원회, C 진화 트랙, 대시보드). 실제 주문 코드는 없습니다.

## 설치 (Windows)

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

`.env` 파일에 Claude API 키를 넣으세요 (B 트랙 AI 위원회용). 업비트 키는 필요 없습니다 (공개 시세만 사용).

```
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...          # G 트랙(Gemini 비교)용. https://aistudio.google.com 에서 발급
```

Gemini 모델 이름은 `config.yaml` 의 `councils.gemini.default.model` 에 있습니다. AI Studio 에서 쓸 수 있는 이름인지 확인하세요.

키는 https://console.anthropic.com → Settings → API Keys 에서 만듭니다 (결제 수단 등록·크레딧 충전 필요).

## 사용법

```bat
python run.py fetch      :: 업비트 4시간봉 4년치 수집 (다시 실행하면 새 봉만 추가)
python run.py council-test   :: AI 위원회 1회 시험 (실제 API 호출, 매매 없음, 비용 수백 원)
python run.py council-test --track G1   :: Gemini 위원회 1회 시험
python run.py evolution      :: C 트랙 버전 이력 (되돌리기: --rollback v1)
python run.py live           :: 실시간 모의매매 시작 (켜 두기). Ctrl+C 로 종료, 다시 켜면 이어서 진행
streamlit run src/report/dashboard.py   :: 대시보드 (브라우저에서 http://localhost:8501)
python run.py backtest   :: 2단계: 후보 전략 4개 비교 → reports/stage2_240m.md
python run.py fetch --unit 60 && python run.py backtest --unit 60   :: 1시간봉 비교
python run.py replay     :: 과거 데이터로 A 트랙을 처음부터 끝까지 실행하고 요약 출력
python run.py runs       :: 실행 목록
python run.py summary --run-id replay-YYYYMMDD-HHMMSS
python -m pytest         :: 테스트
```

### 실시간 모의매매 운영 (Windows)

- `python run.py live` 는 4시간마다(KST 01·05·09·13·17·21시) 봉 마감 15초 뒤에 처리합니다. 타이탄(A5)은 하루 한 번(09시)입니다.
- PC가 절전·종료되어 봉을 놓치면, 다시 켰을 때 놓친 봉으로 소급 매매하지 않습니다. 그 사이 손절가에 닿았다면 켜자마자 현재가로 청산("지연 손절")하고 기록합니다.
- 꺼지지 않게: 제어판 → 전원 옵션에서 절전 "안 함". 또는 작업 스케줄러에 로그온 시 `python run.py live` 실행을 등록하세요.
- 대시보드는 DB를 읽기만 하므로 `live` 가 도는 중에 따로 켜도 됩니다.

결과는 `data/bot.db`(SQLite)에 저장됩니다. [DB Browser for SQLite](https://sqlitebrowser.org/) 같은 프로그램으로
`trades`, `equity`, `signals`, `risk_checks`, `paper_orders` 테이블을 열어 볼 수 있습니다.

설정값은 모두 `config.yaml`에 있습니다.

## 구조

| 폴더 | 내용 |
|---|---|
| `src/data` | 업비트 캔들 수집·저장 |
| `src/indicators` | 지표 계산 (SMA, EMA, RSI, ATR, 볼린저, 돈치안, 거래량 비율) |
| `src/strategies` | 규칙 전략 (공통 인터페이스, 파라미터 허용 범위) |
| `src/paper` | 모의 체결 규칙, 트랙별 계좌 (DB 저장·복원) |
| `src/risk` | 리스크 관문, 캔들 이상 검사 |
| `src/tracks` | 트랙 실행 로직 (봉 단위 처리), A/B/C |
| `src/backtest` | 과거 데이터 재생, 성과 지표 |
| `src/council`, `src/evolution`, `src/report` | 3·4단계 |
