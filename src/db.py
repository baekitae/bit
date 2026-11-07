"""SQLite 연결과 스키마 (DESIGN.md 10장).

모든 트랙 관련 테이블은 run_id 로 구분한다.
- 실시간 모의매매: run_id = "live"
- 과거 데이터 재생(replay): run_id = "replay-..." (실행마다 새로 생성)
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    market TEXT NOT NULL,
    unit   INTEGER NOT NULL,          -- 봉 주기(분)
    ts     INTEGER NOT NULL,          -- 봉 시작 시각, UTC epoch 초
    kst    TEXT NOT NULL,             -- 봉 시작 시각, KST (읽기용)
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume REAL NOT NULL,             -- 거래량 (BTC)
    value  REAL NOT NULL,             -- 거래대금 (KRW)
    PRIMARY KEY (market, unit, ts)
);

CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    event TEXT NOT NULL,
    detail TEXT
);

-- 트랙별 잔고·리스크 상태 (재시작 시 복원용)
CREATE TABLE IF NOT EXISTS tracks (
    run_id TEXT NOT NULL,
    track  TEXT NOT NULL,
    strategy TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    initial_cash REAL NOT NULL,
    cash REAL NOT NULL,
    loss_streak INTEGER NOT NULL DEFAULT 0,
    cooldown_until INTEGER,           -- 이 시각(UTC epoch) 전까지 신규 진입 중단
    day_key TEXT,
    day_start_equity REAL,
    last_equity REAL,
    last_bar_ts INTEGER,              -- 마지막으로 처리한 봉
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (run_id, track)
);

-- 트랙별 현재 보유 포지션 (트랙당 최대 1개)
CREATE TABLE IF NOT EXISTS positions (
    run_id TEXT NOT NULL,
    track  TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_cost REAL NOT NULL,         -- 수수료 포함 매수 총액
    entry_ts INTEGER NOT NULL,
    stop_loss REAL NOT NULL,          -- 현재 손절가 (트레일링 시 올라감)
    entry_order_id INTEGER NOT NULL,
    initial_stop REAL,                -- 진입 시 손절가
    peak_price REAL,                  -- 진입 후 최고가 (트레일링 스탑용)
    trail_pct REAL,
    PRIMARY KEY (run_id, track)
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    track TEXT NOT NULL,
    strategy TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    bar_ts INTEGER NOT NULL,          -- 신호를 계산한 (마감된) 봉
    action TEXT NOT NULL,             -- buy / sell
    strength REAL,
    stop_loss REAL,
    price REAL NOT NULL,              -- 신호 봉 종가
    reason TEXT,
    indicators TEXT                   -- JSON
);

CREATE TABLE IF NOT EXISTS risk_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    track TEXT NOT NULL,
    bar_ts INTEGER NOT NULL,
    signal_id INTEGER,
    approved INTEGER NOT NULL,
    reasons TEXT NOT NULL,            -- JSON 배열 (거부 이유)
    requested_pct REAL,
    approved_pct REAL,
    stop_loss REAL
);

CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    track TEXT NOT NULL,
    signal_id INTEGER,
    side TEXT NOT NULL,               -- buy / sell
    kind TEXT NOT NULL,               -- entry / exit_signal / stop / delayed_stop
    status TEXT NOT NULL,             -- pending / filled / cancelled
    created_bar_ts INTEGER NOT NULL,
    fill_bar_ts INTEGER,
    target_notional REAL,             -- 매수: 목표 투입 금액
    stop_loss REAL,                   -- 매수: 진입 후 적용할 손절가
    ref_price REAL,                   -- 슬리피지 적용 전 기준가
    fill_price REAL,                  -- 슬리피지 적용 후 체결가
    qty REAL,
    fee REAL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    track TEXT NOT NULL,
    entry_order_id INTEGER NOT NULL,
    exit_order_id INTEGER NOT NULL,
    entry_ts INTEGER NOT NULL,
    exit_ts INTEGER NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    entry_cost REAL NOT NULL,
    exit_proceeds REAL NOT NULL,
    pnl REAL NOT NULL,
    pnl_pct REAL NOT NULL,
    exit_kind TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equity (
    run_id TEXT NOT NULL,
    track TEXT NOT NULL,
    bar_ts INTEGER NOT NULL,          -- 이 봉 종가 기준 평가
    cash REAL NOT NULL,
    position_value REAL NOT NULL,
    equity REAL NOT NULL,
    PRIMARY KEY (run_id, track, bar_ts)
);

-- 3단계: AI 위원회 회의 (DESIGN.md 8-5)
CREATE TABLE IF NOT EXISTS council_meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),   -- UTC
    council TEXT,                     -- 위원회 이름 (default=Claude, gemini=비교 위원회)
    run_id TEXT NOT NULL,
    track TEXT NOT NULL,
    signal_id INTEGER,
    bar_ts INTEGER NOT NULL,
    input TEXT,                       -- 위원회 입력 (JSON)
    transcript TEXT,                  -- 역할별 발언 (JSON)
    decision TEXT,                    -- 최종 결정 (JSON)
    providers TEXT,                   -- 역할별 제공자·모델 (JSON)
    used_fallback INTEGER DEFAULT 0,
    prompt_version TEXT,
    gen_settings TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_krw REAL,
    latency_sec REAL,
    error TEXT
);

-- 3단계: A 신호별 B 결정과 A 실제 결과 짝 (DESIGN.md 1-1)
CREATE TABLE IF NOT EXISTS signal_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    a_signal_id INTEGER,              -- A 가 같은 봉에서 신호를 못 냈거나 기록이 없으면 NULL
    b_meeting_id INTEGER,
    b_decision TEXT,
    b_size_multiplier REAL,
    a_trade_id INTEGER,
    a_pnl_pct REAL,
    a_track TEXT,
    b_track TEXT,
    b_trade_id INTEGER,
    b_pnl_pct REAL
);

-- 4단계: C 트랙 전략 버전 (DESIGN.md 9장)
CREATE TABLE IF NOT EXISTS strategy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    track TEXT NOT NULL,
    version TEXT NOT NULL,
    parent_version TEXT,
    strategy TEXT NOT NULL,
    params TEXT NOT NULL,             -- JSON
    status TEXT NOT NULL,             -- active / shadow / retired / rejected / no_change
    change_summary TEXT,
    change_reason TEXT,
    validation TEXT,                  -- 검증 결과 (JSON)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 4단계: C 트랙 진화 루프 상태 (DESIGN.md 9장)
CREATE TABLE IF NOT EXISTS evolution_state (
    run_id TEXT NOT NULL,
    track TEXT NOT NULL,
    phase TEXT NOT NULL,              -- fixed / shadow
    cycle_start_ts INTEGER NOT NULL,  -- 현재 고정 기간 시작
    shadow_version TEXT,
    shadow_start_ts INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,   -- 누적 수정안 시도 횟수 (반복 검증 과최적화 경고용)
    PRIMARY KEY (run_id, track)
);

CREATE INDEX IF NOT EXISTS idx_signals_run ON signals (run_id, track, bar_ts);
CREATE INDEX IF NOT EXISTS idx_orders_run ON paper_orders (run_id, track, status);
CREATE INDEX IF NOT EXISTS idx_trades_run ON trades (run_id, track);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = str(path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """이전 버전 DB에 새 열 추가."""
    # signal_outcomes.a_signal_id 의 NOT NULL 제거 (비어 있을 때만 다시 만든다)
    info = {r[1]: r for r in conn.execute("PRAGMA table_info(signal_outcomes)")}
    if info.get("a_signal_id") is not None and info["a_signal_id"][3] == 1:
        if conn.execute("SELECT COUNT(*) FROM signal_outcomes").fetchone()[0] == 0:
            conn.execute("DROP TABLE signal_outcomes")
            conn.executescript(SCHEMA)
    adds = {
        "positions": [("initial_stop", "REAL"), ("peak_price", "REAL"), ("trail_pct", "REAL")],
        "council_meetings": [("created_at", "TEXT"), ("council", "TEXT")],
        "strategy_versions": [("run_id", "TEXT")],
        "signal_outcomes": [("a_track", "TEXT"), ("b_track", "TEXT"), ("b_trade_id", "INTEGER"),
                            ("b_pnl_pct", "REAL")],
    }
    for table, columns in adds.items():
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, typ in columns:
            if name not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def log_event(conn: sqlite3.Connection, run_id: str, event: str, detail: str | None = None) -> None:
    conn.execute(
        "INSERT INTO run_log (run_id, event, detail) VALUES (?, ?, ?)", (run_id, event, detail)
    )


def backup_db(conn: sqlite3.Connection, out_dir: str | Path, keep: int = 7, tag: str | None = None) -> Path:
    """실행 중에도 안전한 백업 (SQLite 백업 API). 가장 최근 keep 개만 남긴다."""
    from datetime import datetime
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"bot-{tag or datetime.now().strftime('%Y%m%d-%H%M')}.db"
    dst = sqlite3.connect(str(path))
    try:
        conn.backup(dst)
    finally:
        dst.close()
    olds = sorted(out.glob("bot-*.db"))
    for p in olds[:-keep] if keep > 0 else []:
        p.unlink()
    return path
