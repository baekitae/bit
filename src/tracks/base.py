"""트랙 실행 로직 (공통). 봉 하나가 마감될 때마다 `on_bar` 를 한 번 호출한다.

과거 데이터 재생과 실시간 모의매매가 같은 코드를 쓴다.

봉 i 가 마감되었을 때 처리 순서:
  1. 하루 경계(KST 09:00)가 바뀌었으면 당일 시작 자산을 갱신
  2. 직전 봉에서 만든 대기 주문을 봉 i 의 시가에 체결   (= "다음 봉 시가 체결")
  3. 보유 포지션의 손절 확인 (봉 i 의 저가 기준, 진입한 봉 포함), 트레일링 손절가 갱신
  4. 봉 i 까지의 데이터로 신호 계산 → 진입은 리스크 관문 심사 후 대기 주문, 청산은 바로 대기 주문
  5. 봉 i 종가로 자산 평가·기록
  6. 상태 저장 후 커밋 (봉 단위로 원자적 저장)
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from src.db import log_event
from src.paper.account import PaperAccount
from src.paper.fills import Costs, stop_trigger_price
from src.risk.gate import EntryRequest, RiskGate, check_bar
from src.strategies.base import BUY, SELL, Signal, Strategy


@dataclass
class EntryDecision:
    """진입 신호에 대한 트랙의 결정. A는 항상 그대로 따른다. B·C는 AI 위원회가 정한다."""
    size_multiplier: float
    stop_loss: float | None
    error: str | None = None
    note: str = ""


class Track:
    name = "?"

    def __init__(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        cfg: dict[str, Any],
        strategy: Strategy,
        strategy_version: str,
        name: str | None = None,
        unit: int | None = None,
    ):
        self.conn = conn
        self.run_id = run_id
        if name:
            self.name = name
        self.unit = int(unit or cfg["timeframe_minutes"])
        self.window: pd.DataFrame | None = None   # 방금 처리 중인 봉까지의 데이터 (위원회 입력용)
        self.bar_sec = self.unit * 60
        self.day_boundary_sec = int(cfg.get("day_boundary_utc_hour", 0)) * 3600
        self.strategy = strategy
        self.strategy_version = strategy_version
        self.gate = RiskGate(cfg["risk"])
        self.account = PaperAccount(
            conn, run_id, self.name, strategy.name, strategy_version,
            float(cfg["tracks"]["initial_cash"]),
            Costs(float(cfg["costs"]["fee_rate"]), float(cfg["costs"]["slippage_rate"])),
        )

    # B·C 트랙이 재정의한다
    def decide_entry(self, signal: Signal, bar_ts: int, signal_id: int) -> EntryDecision:
        raise NotImplementedError

    def catch_up(self, missed: pd.DataFrame, latest_close: float, note: str) -> None:
        """재시작 처리 (DESIGN.md 12장): 꺼져 있던 동안 놓친 봉들.

        - 소급 매매하지 않는다. 대기 주문은 취소한다.
        - 놓친 봉 중에 손절가를 건드린 봉이 있으면 지금 가격(latest_close)으로 청산하고 "지연 손절"로 기록한다.
        - 놓친 봉들의 자산은 종가로 평가만 기록한다.
        """
        acc = self.account
        st = acc.state
        if missed.empty:
            return
        try:
            for o in acc.pending_orders():
                acc.cancel_order(o["id"], "프로그램 중단으로 체결 봉을 놓쳐 취소")
            pos = acc.position()
            if pos is not None and float(missed["low"].min()) <= pos.stop_loss:
                last_ts = int(missed["ts"].iloc[-1])
                oid = acc.place_order("sell", "delayed_stop", last_ts, note="중단 중 손절가 도달 → 재시작 시 청산")
                _, pnl = acc.close_position(oid, last_ts, latest_close, "delayed_stop")
                self._after_trade(pnl, last_ts + self.bar_sec)
            for r in missed.itertuples():
                st.day_key = self.day_key(int(r.ts))
                acc.record_equity(int(r.ts), float(r.close))
            st.last_bar_ts = int(missed["ts"].iloc[-1])
            acc.save_state()
            log_event(self.conn, self.run_id, "missed_bars",
                      f"{self.name}: {len(missed)}개 봉 건너뜀 ({note})")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def day_key(self, bar_ts: int) -> str:
        return datetime.fromtimestamp(bar_ts - self.day_boundary_sec, tz=timezone.utc).strftime("%Y-%m-%d")

    def on_bar(
        self,
        window: pd.DataFrame,
        prev_ts: int | None,
        now_ts: int | None = None,
        max_delay_bars: float | None = None,
        system_error: str | None = None,
    ) -> bool:
        """방금 마감된 봉(window 마지막 행)을 처리한다. 이미 처리한 봉이면 False."""
        acc = self.account
        st = acc.state
        self.window = window
        bar = window.iloc[-1]
        ts = int(bar["ts"])
        if st.last_bar_ts is not None and ts <= st.last_bar_ts:
            return False
        bar_d = {k: float(bar[k]) for k in ("open", "high", "low", "close")}
        bar_d["ts"] = ts
        data_ok, data_reason = check_bar(bar_d, prev_ts, self.unit, now_ts, max_delay_bars)
        close_ts = ts + self.bar_sec

        try:
            # 1. 하루 경계
            dk = self.day_key(ts)
            if dk != st.day_key:
                st.day_key = dk
                st.day_start_equity = st.last_equity if st.last_equity is not None else st.cash

            # 2. 대기 주문 체결 (시가)
            for o in acc.pending_orders():
                if o["side"] == "buy":
                    if not data_ok:
                        acc.cancel_order(o["id"], f"체결 봉 데이터 이상: {data_reason}")
                        continue
                    acc.fill_buy(o["id"], ts, bar_d["open"], o["target_notional"], o["stop_loss"],
                                 self.strategy.trail_pct)
                else:
                    if acc.position() is None:
                        acc.cancel_order(o["id"], "청산할 포지션 없음")
                        continue
                    _, pnl = acc.close_position(o["id"], ts, bar_d["open"], o["kind"])
                    self._after_trade(pnl, close_ts)

            # 3. 손절 (AI와 무관하게 항상)
            pos = acc.position()
            if pos is not None:
                ref = stop_trigger_price(bar_d["open"], bar_d["low"], pos.stop_loss)
                if ref is not None:
                    kind = "trail_stop" if pos.stop_loss > pos.initial_stop else "stop"
                    oid = acc.place_order("sell", kind, ts, note=f"손절가 {pos.stop_loss:.0f}")
                    _, pnl = acc.close_position(oid, ts, ref, kind)
                    self._after_trade(pnl, close_ts)
            # 트레일링 스탑: 이 봉의 최고가를 반영해 다음 봉부터 적용
            acc.update_trailing(bar_d["high"])

            # 4. 신호
            sig = self.strategy.generate(window)
            if sig.action == BUY:
                self._handle_buy(sig, ts, close_ts, data_ok, data_reason, system_error, bar_d["close"])
            elif sig.action == SELL:
                sig_id = acc.record_signal(self.strategy.name, self.strategy_version, ts, sig)
                has_pending_sell = any(o["side"] == "sell" for o in acc.pending_orders())
                if acc.position() is not None and not has_pending_sell:
                    acc.place_order("sell", "exit_signal", ts, signal_id=sig_id, note=sig.reason)

            # 5. 자산 평가
            acc.record_equity(ts, bar_d["close"])

            # 6. 저장
            st.last_bar_ts = ts
            acc.save_state()
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return True

    def _handle_buy(self, sig: Signal, ts: int, close_ts: int, data_ok: bool, data_reason: str,
                    system_error: str | None, close_price: float) -> None:
        acc = self.account
        st = acc.state
        sig_id = acc.record_signal(self.strategy.name, self.strategy_version, ts, sig)
        has_position = acc.position() is not None
        has_pending = bool(acc.pending_orders())

        decision = None
        if not has_position and not has_pending and data_ok and not system_error:
            try:
                decision = self.decide_entry(sig, ts, sig_id)
            except Exception as e:  # 결정 실패 → 신규 진입 안 함
                decision = EntryDecision(0.0, None, error=f"결정 오류: {e}")
        if decision is None:
            decision = EntryDecision(1.0, sig.stop_loss)  # 관문이 거부 이유를 기록하도록

        equity = acc.equity_at(close_price)
        req = EntryRequest(
            price=close_price,
            stop_loss=decision.stop_loss,
            size_multiplier=decision.size_multiplier,
            has_position=has_position,
            has_pending_order=has_pending,
            equity=equity,
            day_start_equity=st.day_start_equity or equity,
            decision_ts=close_ts,
            cooldown_until=st.cooldown_until,
            data_ok=data_ok,
            data_reason=data_reason,
            system_error=system_error or decision.error,
        )
        result = self.gate.check_entry(req)
        acc.record_risk_check(ts, sig_id, result, decision.stop_loss)
        if result.approved:
            acc.place_order("buy", "entry", ts, signal_id=sig_id,
                            target_notional=result.approved_pct * equity,
                            stop_loss=decision.stop_loss, note=sig.reason)

    def _after_trade(self, pnl: float, close_ts: int) -> None:
        st = self.account.state
        streak, cooldown = self.gate.after_trade(st.loss_streak, pnl, close_ts)
        st.loss_streak = streak
        if cooldown is not None:
            st.cooldown_until = cooldown
