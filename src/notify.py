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

    def send(self, text: str, retries: int = 3, timeout: float = 30.0) -> bool:
        """전송. 네트워크 지연·일시 오류는 몇 번 다시 시도한다. 성공하면 True."""
        import time
        last = ""
        for attempt in range(retries):
            try:
                r = self.session.post(self.URL.format(token=self.token),
                                      json={"chat_id": self.chat_id, "text": text[:4000]}, timeout=timeout)
            except Exception as e:  # 시간 초과, 연결 오류 등
                last = f"네트워크 오류 {type(e).__name__} (서버에서 api.telegram.org 연결 확인 필요)"
            else:
                if r.status_code < 400:
                    return True
                if r.status_code in (400, 401, 403, 404):
                    hint = {401: "토큰이 틀림", 404: "토큰이 틀림", 400: "채팅 ID 가 틀리거나 봇과 대화를 시작하지 않음",
                            403: "봇이 차단됨 (텔레그램에서 봇 대화창의 시작을 누르세요)"}[r.status_code]
                    self.log(f"텔레그램 전송 실패 {r.status_code}: {hint}")
                    return False          # 설정 문제는 다시 시도해도 안 됨
                last = f"텔레그램 서버 오류 {r.status_code}"
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
        self.log(f"텔레그램 전송 실패: {last}")
        return False


    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        """새 메시지 받기 (롱 폴링: 메시지가 없으면 timeout 초까지 기다림). 실패하면 빈 목록."""
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": json.dumps(["message"])}
        if offset is not None:
            params["offset"] = offset
        try:
            r = self.session.get(self.URL.replace("sendMessage", "getUpdates").format(token=self.token),
                                 params=params, timeout=timeout + 15)
            if r.status_code >= 400:
                return []
            data = r.json()
            return data.get("result", []) if data.get("ok") else []
        except Exception:
            return []


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


class Commands:
    """텔레그램 명령에 답한다. 설정된 채팅 ID 에서 온 메시지에만 답하고, 읽기만 한다 (매매에 영향 없음)."""

    HELP = ("명령 목록\n"
            "/status  트랙별 자산·수익률, 보유 포지션\n"
            "/today   오늘(KST 09시 이후) 체결과 위원회 결정\n"
            "/cost    AI 비용 합계\n"
            "/help    이 목록")

    def __init__(self, conn: sqlite3.Connection, run_id: str, cfg: dict[str, Any], sender: Telegram,
                 now: Callable[[], float] | None = None):
        import time
        self.conn, self.run_id, self.cfg, self.sender = conn, run_id, cfg, sender
        self.now = now or time.time
        self.offset: int | None = None
        self.boundary = int(cfg.get("day_boundary_utc_hour", 0)) * 3600

    def skip_backlog(self) -> None:
        """시작 전에 쌓인 명령은 무시한다."""
        ups = self.sender.get_updates(-1, 0)
        if ups:
            self.offset = ups[-1]["update_id"] + 1

    def poll(self, timeout: int) -> int:
        """명령을 기다렸다가 처리한다. 처리한 명령 수."""
        n = 0
        for u in self.sender.get_updates(self.offset, timeout):
            self.offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != str(self.sender.chat_id):
                continue                      # 다른 사람의 메시지는 무시
            reply = self.handle((msg.get("text") or "").strip())
            if reply:
                self.sender.send(reply, retries=2)
                n += 1
        return n

    def handle(self, text: str) -> str | None:
        cmd = text.split()[0].split("@")[0].lower() if text else ""
        try:
            return {"/status": self.status, "/today": self.today, "/cost": self.cost,
                    "/help": lambda: self.HELP, "/start": lambda: self.HELP}.get(cmd, lambda: self.HELP)()
        except Exception as e:
            return f"명령 처리 중 오류: {type(e).__name__}"

    def _day_start(self) -> int:
        now = int(self.now())
        return (now - self.boundary) // 86400 * 86400 + self.boundary

    def status(self) -> str:
        rows = self.conn.execute(
            "SELECT track, last_equity, cash, initial_cash, last_bar_ts FROM tracks WHERE run_id=? ORDER BY track",
            (self.run_id,)).fetchall()
        if not rows:
            return "아직 기록이 없습니다."
        start = self.conn.execute("SELECT MIN(created_at) FROM run_log WHERE run_id=? AND event='comparison_start'",
                                  (self.run_id,)).fetchone()[0]
        days = ""
        if start:
            d0 = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
            days = f" · 비교 {(datetime.fromtimestamp(self.now(), tz=timezone.utc) - d0).days + 1}일째"
        last = max((r["last_bar_ts"] or 0) for r in rows)
        out = [f"📈 상태 (마지막 봉 {_kst(last)} KST{days})"]
        for r in rows:
            eq = r["last_equity"] or r["cash"]
            out.append(f"{r['track']:<6}{eq:>12,.0f}원 {eq / r['initial_cash'] - 1:+.2%}")
        pos = self.conn.execute("SELECT track, entry_price, stop_loss FROM positions WHERE run_id=? ORDER BY track",
                                (self.run_id,)).fetchall()
        if pos:
            out.append("보유 중:")
            out += [f"  {p['track']} 진입 {p['entry_price']:,.0f} · 손절 {p['stop_loss']:,.0f}" for p in pos]
        else:
            out.append("보유 중인 포지션 없음")
        return "\n".join(out)

    def today(self) -> str:
        since = self._day_start()
        out = [f"🗓 오늘 ({_kst(since)} KST 이후)"]
        trades = self.conn.execute(
            """SELECT o.track, o.side, o.kind, o.fill_price, t.pnl_pct FROM paper_orders o
               LEFT JOIN trades t ON t.exit_order_id = o.id
               WHERE o.run_id=? AND o.status='filled' AND o.fill_bar_ts >= ? ORDER BY o.id""",
            (self.run_id, since - 4 * 3600)).fetchall()
        for t in trades:
            if t["side"] == "buy":
                out.append(f"🟢 {t['track']} 진입 {t['fill_price']:,.0f}")
            else:
                pnl = f" {t['pnl_pct']:+.2%}" if t["pnl_pct"] is not None else ""
                out.append(f"🔴 {t['track']} {KIND_LABELS.get(t['kind'], t['kind'])} {t['fill_price']:,.0f}{pnl}")
        start_txt = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        meets = self.conn.execute(
            "SELECT track, decision, error, cost_krw FROM council_meetings WHERE run_id=? AND created_at >= ? ORDER BY id",
            (self.run_id, start_txt)).fetchall()
        for m in meets:
            if m["error"]:
                out.append(f"🤖 {m['track']} 실패→건너뜀")
            else:
                a = json.loads(m["decision"] or "{}").get("applied", {})
                out.append(f"🤖 {m['track']} {DECISION_LABELS.get(a.get('decision'), a.get('decision'))}"
                           f" (x{a.get('size_multiplier', 0):.2f}, {m['cost_krw'] or 0:,.0f}원)")
        if len(out) == 1:
            out.append("오늘은 아직 체결도 회의도 없습니다.")
        return "\n".join(out)

    def cost(self) -> str:
        rows = self.conn.execute(
            """SELECT COALESCE(council, 'default') AS c, COUNT(*) AS n, COALESCE(SUM(cost_krw), 0) AS krw,
                      SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS err
               FROM council_meetings WHERE run_id=? GROUP BY 1""", (self.run_id,)).fetchall()
        if not rows:
            return "아직 위원회 회의가 없습니다."
        names = {"default": "Claude", "gemini": "Gemini"}
        out = ["💰 AI 비용 (누적)"]
        total = 0.0
        for r in rows:
            total += r["krw"]
            out.append(f"{names.get(r['c'], r['c'])}: 회의 {r['n']}회 (실패 {r['err']}) · {r['krw']:,.0f}원")
        out.append(f"합계 {total:,.0f}원")
        return "\n".join(out)
