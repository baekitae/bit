"""업비트 공개 시세 API로 분봉 캔들 수집·저장.

- 키가 필요 없는 공개 API만 사용한다.
- 진행 중인(아직 마감되지 않은) 봉은 저장하지 않는다. → 미래 데이터 참조 방지
- 업비트는 최신 봉부터 내려주며, `to` 파라미터(배타적)로 과거 페이지를 받는다.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pandas as pd
import requests

API_URL = "https://api.upbit.com/v1/candles/minutes/{unit}"
PAGE_SIZE = 200  # 업비트 최대값
KST = timezone(timedelta(hours=9))


def _parse_utc(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())


def _fmt_to(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def kst_str(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=KST).strftime("%Y-%m-%d %H:%M")


def parse_candle(row: dict[str, Any]) -> dict[str, Any]:
    ts = _parse_utc(row["candle_date_time_utc"])
    return {
        "ts": ts,
        "kst": kst_str(ts),
        "open": float(row["opening_price"]),
        "high": float(row["high_price"]),
        "low": float(row["low_price"]),
        "close": float(row["trade_price"]),
        "volume": float(row["candle_acc_trade_volume"]),
        "value": float(row["candle_acc_trade_price"]),
    }


class UpbitClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        interval_sec: float = 0.12,
        max_retries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.session = session or requests.Session()
        self.interval_sec = interval_sec
        self.max_retries = max_retries
        self.sleep = sleep

    def fetch_page(self, market: str, unit: int, to_ts: int | None = None) -> list[dict[str, Any]]:
        """`to_ts` 이전(배타적) 캔들 최대 200개를 최신순으로 반환."""
        params: dict[str, Any] = {"market": market, "count": PAGE_SIZE}
        if to_ts is not None:
            params["to"] = _fmt_to(to_ts)
        url = API_URL.format(unit=unit)
        delay = 1.0
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, params=params, timeout=10)
            except requests.RequestException:
                if attempt == self.max_retries - 1:
                    raise
                self.sleep(delay)
                delay *= 2
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.max_retries - 1:
                    resp.raise_for_status()
                self.sleep(delay)
                delay *= 2
                continue
            resp.raise_for_status()
            self.sleep(self.interval_sec)
            return [parse_candle(r) for r in resp.json()]
        raise RuntimeError("unreachable")


def _store(conn: sqlite3.Connection, market: str, unit: int, rows: list[dict[str, Any]]) -> int:
    conn.executemany(
        """INSERT OR REPLACE INTO candles (market, unit, ts, kst, open, high, low, close, volume, value)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (market, unit, r["ts"], r["kst"], r["open"], r["high"], r["low"], r["close"], r["volume"], r["value"])
            for r in rows
        ],
    )
    return len(rows)


def _ts_range(conn: sqlite3.Connection, market: str, unit: int) -> tuple[int | None, int | None]:
    row = conn.execute(
        "SELECT MIN(ts), MAX(ts) FROM candles WHERE market=? AND unit=?", (market, unit)
    ).fetchone()
    return row[0], row[1]


def sync_candles(
    conn: sqlite3.Connection,
    client: UpbitClient,
    market: str,
    unit: int,
    since_ts: int,
    now_ts: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> int:
    """`since_ts` 이후 마감된 캔들을 모두 DB에 채운다. 저장한 개수를 반환.

    1) 최신 → DB의 가장 최근 봉까지 (신규 봉 업데이트)
    2) DB의 가장 오래된 봉 → since_ts 까지 (과거 데이터 보충)
    """
    now_ts = int(time.time()) if now_ts is None else now_ts
    bar_sec = unit * 60
    min_ts, max_ts = _ts_range(conn, market, unit)
    total = 0

    def page_down(to_ts: int | None, stop_at: int) -> int:
        n = 0
        cursor = to_ts
        while True:
            rows = client.fetch_page(market, unit, cursor)
            if not rows:
                break
            closed = [r for r in rows if r["ts"] + bar_sec <= now_ts and r["ts"] >= since_ts]
            n += _store(conn, market, unit, closed)
            conn.commit()
            oldest = min(r["ts"] for r in rows)
            if progress:
                progress(f"  {kst_str(oldest)} 까지 수집 (누적 {n}개)")
            if oldest <= stop_at:
                break
            cursor = oldest
        return n

    # 1) 최신 쪽
    total += page_down(None, max_ts if max_ts is not None else since_ts)
    # 2) 과거 쪽
    min_ts, _ = _ts_range(conn, market, unit)
    if min_ts is not None and min_ts - bar_sec >= since_ts:
        total += page_down(min_ts, since_ts)
    return total


def since_years(years: float, now_ts: int | None = None) -> int:
    now_ts = int(time.time()) if now_ts is None else now_ts
    return now_ts - int(years * 365.25 * 86400)


def load_candles(
    conn: sqlite3.Connection,
    market: str,
    unit: int,
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> pd.DataFrame:
    q = "SELECT ts, kst, open, high, low, close, volume, value FROM candles WHERE market=? AND unit=?"
    args: list[Any] = [market, unit]
    if start_ts is not None:
        q += " AND ts >= ?"
        args.append(start_ts)
    if end_ts is not None:
        q += " AND ts <= ?"
        args.append(end_ts)
    q += " ORDER BY ts"
    df = pd.read_sql_query(q, conn, params=args)
    return df.reset_index(drop=True)


def find_gaps(df: pd.DataFrame, unit: int) -> list[tuple[int, int]]:
    """연속되지 않는 구간 [(직전 봉 ts, 다음 봉 ts), ...]"""
    if len(df) < 2:
        return []
    ts = df["ts"].to_numpy()
    diffs = ts[1:] - ts[:-1]
    idx = (diffs != unit * 60).nonzero()[0]
    return [(int(ts[i]), int(ts[i + 1])) for i in idx]
