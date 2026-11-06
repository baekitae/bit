# BTC AI 위원회 모의매매 봇

업비트 KRW-BTC에서 세 가지 방식(A: 규칙 전략, B: 규칙 + AI 필터, C: 규칙 + AI 필터 + 전략 진화)을
같은 조건에서 모의매매로 비교하는 프로젝트입니다. 설계는 [DESIGN.md](DESIGN.md)를 기준으로 합니다.

**현재: 1단계** (뼈대·데이터·모의매매 엔진, A 트랙만 작동). 실제 주문 코드는 없습니다.

## 설치 (Windows)

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

1단계는 업비트 공개 시세만 쓰므로 `.env`에 아무 키도 넣지 않아도 됩니다.

## 사용법

```bat
python run.py fetch      :: 업비트 4시간봉 4년치 수집 (다시 실행하면 새 봉만 추가)
python run.py replay     :: 과거 데이터로 A 트랙을 처음부터 끝까지 실행하고 요약 출력
python run.py runs       :: 실행 목록
python run.py summary --run-id replay-YYYYMMDD-HHMMSS
python -m pytest         :: 테스트
```

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
