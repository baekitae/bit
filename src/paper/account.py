"""트랙별 모의 계좌: 잔고·포지션·주문·거래·자산곡선을 DB에 저장하고 복원한다.

계좌 상태는 항상 DB가 기준이다 (DESIGN.md 원칙 6: 재시작해도 상태 유지).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from src.paper.fills import Costs, Fill, buy_fill, sell_fill


@dataclass
class TrackState:
    cash: float
    initial_cash: float
    loss_streak: int
    cooldown_until: int | None
    day_key: str | None
    day_start_equity: float | None
    last_equity: float | None
    last_bar_ts: int | None


@dataclass
class Position:
    qty: float
    entry_price: float
    entry_cost: float
    entry_ts: int
    stop_loss: float
    entry_order_id: int
    initial_stop: float
    peak_price: float
    trail_pct: float | None


class PaperAccount:
    def __init__(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        track: str,
        strategy: str,
        strategy_version: str,
        initial_cash: float,
        costs: Costs,
    ):
        self.conn = conn
        self.run_id = run_id
        self.track = track
        self.costs = costs
        row = conn.execute(
            "SELECT * FROM tracks WHERE run_id=? AND track=?", (run_id, track)
        ).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO tracks (run_id, track, strategy, strategy_version, initial_cash, cash)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, track, strategy, strategy_version, initial_cash, initial_cash),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM tracks WHERE run_id=? AND track=?", (run_id, track)
            ).fetchone()
        self.state = TrackState(
            cash=row["cash"],
            initial_cash=row["initial_cash"],
            loss_streak=row["loss_streak"],
            cooldown_until=row["cooldown_until"],
            day_key=row["day_key"],
            day_start_equity=row["day_start_equity"],
            last_equity=row["last_equity"],
            last_bar_ts=row["last_bar_ts"],
        )

    # ---- 조회 ----
    @property
    def key(self) -> tuple[str, str]:
        return self.run_id, self.track

    def position(self) -> Position | None:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE run_id=? AND track=?", self.key
        ).fetchone()
        if row is None:
            return None
        return Position(row["qty"], row["entry_price"], row["entry_cost"], row["entry_ts"],
                        row["stop_loss"], row["entry_order_id"],
                        row["initial_stop"] if row["initial_stop"] is not None else row["stop_loss"],
                        row["peak_price"] if row["peak_price"] is not None else row["entry_price"],
                        row["trail_pct"])

    def pending_orders(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM paper_orders WHERE run_id=? AND track=? AND status='pending' ORDER BY id",
            self.key,
        ).fetchall()

    def equity_at(self, price: float) -> float:
        pos = self.position()
        return self.state.cash + (pos.qty * price if pos else 0.0)

    # ---- 기록 ----
    def record_signal(self, strategy: str, version: str, bar_ts: int, sig: Any) -> int:
        cur = self.conn.execute(
            """INSERT INTO signals (run_id, track, strategy, strategy_version, bar_ts, action,
                                    strength, stop_loss, price, reason, indicators)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (*self.key, strategy, version, bar_ts, sig.action, sig.strength, sig.stop_loss,
             sig.price, sig.reason, json.dumps(sig.indicators, ensure_ascii=False)),
        )
        return int(cur.lastrowid)

    def record_risk_check(self, bar_ts: int, signal_id: int | None, result: Any, stop_loss: float | None) -> None:
        self.conn.execute(
            """INSERT INTO risk_checks (run_id, track, bar_ts, signal_id, approved, reasons,
                                        requested_pct, approved_pct, stop_loss)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (*self.key, bar_ts, signal_id, int(result.approved),
             json.dumps(result.reasons, ensure_ascii=False), result.requested_pct,
             result.approved_pct, stop_loss),
        )

    def place_order(self, side: str, kind: str, bar_ts: int, signal_id: int | None = None,
                    target_notional: float | None = None, stop_loss: float | None = None,
                    note: str | None = None) -> int:
        cur = self.conn.execute(
            """INSERT INTO paper_orders (run_id, track, signal_id, side, kind, status, created_bar_ts,
                                         target_notional, stop_loss, note)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (*self.key, signal_id, side, kind, bar_ts, target_notional, stop_loss, note),
        )
        return int(cur.lastrowid)

    def cancel_order(self, order_id: int, note: str) -> None:
        self.conn.execute(
            "UPDATE paper_orders SET status='cancelled', note=? WHERE id=?", (note, order_id)
        )

    def _mark_filled(self, order_id: int, bar_ts: int, f: Fill) -> None:
        self.conn.execute(
            """UPDATE paper_orders SET status='filled', fill_bar_ts=?, ref_price=?, fill_price=?,
                                       qty=?, fee=? WHERE id=?""",
            (bar_ts, f.ref_price, f.price, f.qty, f.fee, order_id),
        )

    # ---- 체결 ----
    def fill_buy(self, order_id: int, bar_ts: int, ref_price: float, target_notional: float,
                 stop_loss: float, trail_pct: float | None = None) -> Fill | None:
        spend = min(target_notional, self.state.cash)
        if spend <= 0:
            self.cancel_order(order_id, "현금 부족")
            return None
        f = buy_fill(ref_price, spend, self.costs)
        self.state.cash += f.cash_delta
        self._mark_filled(order_id, bar_ts, f)
        self.conn.execute(
            """INSERT INTO positions (run_id, track, qty, entry_price, entry_cost, entry_ts,
                                      stop_loss, entry_order_id, initial_stop, peak_price, trail_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (*self.key, f.qty, f.price, -f.cash_delta, bar_ts, stop_loss, order_id,
             stop_loss, f.price, trail_pct),
        )
        return f

    def update_trailing(self, high: float) -> None:
        """봉 마감 후 최고가 갱신과 트레일링 손절가 상향 (내리지는 않음)."""
        pos = self.position()
        if pos is None or not pos.trail_pct:
            return
        peak = max(pos.peak_price, high)
        stop = max(pos.stop_loss, peak * (1 - pos.trail_pct))
        self.conn.execute(
            "UPDATE positions SET peak_price=?, stop_loss=? WHERE run_id=? AND track=?",
            (peak, stop, *self.key),
        )

    def close_position(self, order_id: int, bar_ts: int, ref_price: float, exit_kind: str) -> tuple[Fill, float]:
        """보유 포지션 전량 청산. (체결, 손익) 반환."""
        pos = self.position()
        if pos is None:
            raise RuntimeError("청산할 포지션이 없습니다")
        f = sell_fill(ref_price, pos.qty, self.costs)
        self.state.cash += f.cash_delta
        self._mark_filled(order_id, bar_ts, f)
        pnl = f.cash_delta - pos.entry_cost
        self.conn.execute(
            """INSERT INTO trades (run_id, track, entry_order_id, exit_order_id, entry_ts, exit_ts,
                                   qty, entry_price, exit_price, entry_cost, exit_proceeds,
                                   pnl, pnl_pct, exit_kind)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (*self.key, pos.entry_order_id, order_id, pos.entry_ts, bar_ts, pos.qty,
             pos.entry_price, f.price, pos.entry_cost, f.cash_delta, pnl, pnl / pos.entry_cost,
             exit_kind),
        )
        self.conn.execute("DELETE FROM positions WHERE run_id=? AND track=?", self.key)
        return f, pnl

    def record_equity(self, bar_ts: int, close_price: float) -> float:
        pos = self.position()
        pos_value = pos.qty * close_price if pos else 0.0
        eq = self.state.cash + pos_value
        self.conn.execute(
            """INSERT OR REPLACE INTO equity (run_id, track, bar_ts, cash, position_value, equity)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (*self.key, bar_ts, self.state.cash, pos_value, eq),
        )
        self.state.last_equity = eq
        return eq

    def save_state(self) -> None:
        s = self.state
        self.conn.execute(
            """UPDATE tracks SET cash=?, loss_streak=?, cooldown_until=?, day_key=?,
                                 day_start_equity=?, last_equity=?, last_bar_ts=?,
                                 updated_at=datetime('now')
               WHERE run_id=? AND track=?""",
            (s.cash, s.loss_streak, s.cooldown_until, s.day_key, s.day_start_equity,
             s.last_equity, s.last_bar_ts, *self.key),
        )
