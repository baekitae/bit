"""텔레그램 알림 (선택). .env 에 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 가 있을 때만 보낸다.

알림 전송 실패는 매매 루프에 영향을 주지 않는다 (로그만 남기고 넘어간다).
알림은 "보고"일 뿐 어떤 결정도 바꾸지 않는다.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

KST = timezone(timedelta(hours=9))
KIND_LABELS = {"entry": "진입", "exit_signal": "신호 청산", "stop": "손절", "trail_stop": "트레일링 손절",
               "delayed_stop": "지연 손절"}
DECISION_LABELS = {"follow": "따름", "reduce": "비중 축소", "skip": "건너뜀"}


class Telegram:
    URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: str, chat_id: str, session: Any = None, log: Callable[[str], None] = print):
        import requests
        self.token, self.chat_id = token, chat_id
        self.session = session or requests.Session()
        self.log = log

    @classmethod
    def from_env(cls, **kw: Any) -> "Telegram | None":
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        return cls(token, chat, **kw) if token and chat else None

    def send(self, text: str) -> bool:
        try:
            r = self.session.post(self.URL.format(token=self.token),
                                  json={"chat_id": self.chat_id, "text": text[:4000]}, timeout=10)
            if r.status_code >= 400:
                self.log(f"텔레그램 전송 실패 {r.status_code}")
                return False
            return True
        except Exception as e:  # 네트워크 오류 등
            self.log(f"텔레그램 전송 실패: {type(e).__name__}")
            return False


def _kst(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=KST).strftime("%m-%d %H:%M")


class Notifier:
    """매 회차 뒤 DB 에서 새로 생긴 일(체결, 회의, 오류)을 모아 한 메시지로 보낸다."""

    def __init__(self, conn: sqlite3.Connection, run_id: str, cfg: dict[str, Any], sender: Telegram):
        self.conn, self.run_id, self.sender = conn, run_id, sender
        n = cfg.get("notify", {})
        self.opts = {k: bool(n.get(k, True)) for k in ("trades", "meetings", "errors", "daily_summary")}
        self.boundary = int(cfg.get("day_boundary_utc_hour", 0)) * 3600
        self.last = {t: self._max_id(t) for t in ("paper_orders", "council_meetings", "run_log")}
        self.last_summary_day: str | None = None

    def _max_id(self, table: str) -> int:
        return self.conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table} WHERE run_id=?",
                                 (self.run_id,)).fetchone()[0]

    def _new(self, table: str, where: str = "") -> list[sqlite3.Row]:
        rows = self.conn.execute(f"SELECT * FROM {table} WHERE run_id=? AND id>? {where} ORDER BY id",
                                 (self.run_id, self.last[table])).fetchall()
        self.last[table] = self._max_id(table)
        return rows

    def collect(self) -> list[str]:
        lines: list[str] = []
        orders = self._new("paper_orders", "AND status='filled'")
        if self.opts["trades"]:
            for o in orders:
                if o["side"] == "buy":
                    lines.append(f"🟢 {o['track']} 진입 {o['fill_price']:,.0f}원 · {o['qty'] * o['fill_price']:,.0f}원어치")
                else:
                    t = self.conn.execute("SELECT pnl_pct FROM trades WHERE run_id=? AND exit_order_id=?",
                                          (self.run_id, o["id"])).fetchone()
                    pnl = f" · {t['pnl_pct']:+.2%}" if t else ""
                    lines.append(f"🔴 {o['track']} {KIND_LABELS.get(o['kind'], o['kind'])} {o['fill_price']:,.0f}원{pnl}")
        meetings = self._new("council_meetings")
        if self.opts["meetings"]:
            for m in meetings:
                if m["error"]:
                    lines.append(f"🤖 {m['track']} 위원회 실패 → 건너뜀 ({str(m['error'])[:80]})")
                    continue
                d = json.loads(m["decision"] or "{}")
                a, raw = d.get("applied", {}), d.get("raw") or {}
                lines.append(f"🤖 {m['track']} 위원회: {DECISION_LABELS.get(a.get('decision'), a.get('decision'))}"
                             f" (비중 x{a.get('size_multiplier', 0):.2f}, 확신 {a.get('confidence', 0):.2f},"
                             f" {m['cost_krw'] or 0:,.0f}원)\n   {str(raw.get('reasoning', ''))[:150]}")
        logs = self._new("run_log")
        if self.opts["errors"]:
            for r in logs:
                if r["event"] in ("data_error", "evolution_error", "step_error"):
                    lines.append(f"⚠️ {r['detail'][:200]}")
                elif r["event"] == "evolution":
                    lines.append(f"🧬 {r['detail'][:200]}")
        return lines

    def daily_summary(self, now_ts: int) -> str | None:
        day = datetime.fromtimestamp(now_ts - self.boundary, tz=timezone.utc).strftime("%Y-%m-%d")
        if not self.opts["daily_summary"] or day == self.last_summary_day:
            return None
        first = self.last_summary_day is None
        self.last_summary_day = day
        if first:
            return None      # 시작 직후에는 보내지 않고, 다음 하루 경계부터
        rows = self.conn.execute(
            "SELECT track, last_equity, cash, initial_cash FROM tracks WHERE run_id=? ORDER BY track",
            (self.run_id,)).fetchall()
        cost = self.conn.execute("SELECT COALESCE(SUM(cost_krw), 0) FROM council_meetings WHERE run_id=?",
                                 (self.run_id,)).fetchone()[0]
        out = [f"📊 하루 요약 ({day})"]
        for r in rows:
            eq = r["last_equity"] or r["cash"]
            out.append(f"{r['track']:<6} {eq:>11,.0f}원 {eq / r['initial_cash'] - 1:+.2%}")
        out.append(f"누적 AI 비용 {cost:,.0f}원")
        return "\n".join(out)

    def after_step(self, now_ts: int) -> None:
        msgs = []
        lines = self.collect()
        if lines:
            msgs.append("\n".join(lines))
        s = self.daily_summary(now_ts)
        if s:
            msgs.append(s)
        for m in msgs:
            self.sender.send(m)
