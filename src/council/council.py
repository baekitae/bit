"""AI 위원회 회의 진행과 결정 검증 (DESIGN.md 8장).

- 세 위원(추세, 국면·위험, 레드팀)은 서로의 의견을 보지 않고 동시에 판단한다.
- 위원장이 세 의견을 받아 최종 결정을 낸다.
- 어떤 단계든 실패(오류, 시간 초과, 거부, 형식 오류)하면 결정은 skip(신규 진입 안 함)이다.
- AI 결정은 코드가 다시 검증한다: 비중은 0~1로 잘라내고, 손절가는 좁히는 방향만 받는다.
- 회의 전문, 모델, 토큰, 비용을 council_meetings 에 저장한다.
"""
from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from src.council import prompts
from src.council.llm import LLMClient, LLMError, LLMResponse, make_client

MEMBERS = ("trend", "regime", "red_team")


@dataclass
class CouncilDecision:
    decision: str                 # follow / skip / reduce
    size_multiplier: float
    stop_loss: float | None       # 실제 적용할 손절가 (규칙 손절가 또는 더 좁은 AI 손절가)
    confidence: float
    reasoning: str
    dissent: str
    error: str | None = None
    meeting_id: int | None = None
    notes: list[str] = field(default_factory=list)


def validate_decision(raw: dict[str, Any], rule_stop: float | None, price: float) -> CouncilDecision:
    """AI 결정을 규칙에 맞게 고친다 (DESIGN.md 8-4)."""
    notes: list[str] = []
    d = raw.get("decision")
    if d not in ("follow", "skip", "reduce"):
        raise LLMError(f"알 수 없는 decision: {d!r}")
    try:
        mult = float(raw.get("size_multiplier", 0.0))
    except (TypeError, ValueError):
        mult = 0.0
    if d == "follow":
        mult = 1.0
    elif d == "skip":
        mult = 0.0
    else:
        clamped = min(max(mult, 0.0), 1.0)
        if clamped != mult:
            notes.append(f"size_multiplier {mult} → {clamped} (0~1로 제한)")
        mult = clamped
        if mult == 0.0:
            d = "skip"
            notes.append("reduce 인데 비중 0 → skip 으로 처리")
        elif mult == 1.0:
            d = "follow"
            notes.append("reduce 인데 비중 1 → follow 로 처리")

    stop = rule_stop
    try:
        ai_stop = float(raw.get("stop_loss") or 0.0)
    except (TypeError, ValueError):
        ai_stop = 0.0
    if ai_stop > 0:
        if rule_stop is not None and rule_stop < ai_stop < price:
            stop = ai_stop
            notes.append(f"AI 손절가 적용 (규칙 {rule_stop:,.0f} → {ai_stop:,.0f}, 더 좁게)")
        else:
            notes.append(f"AI 손절가 {ai_stop:,.0f} 무시 (규칙 손절가보다 넓거나 현재가 이상)")

    try:
        conf = min(max(float(raw.get("confidence", 0.0)), 0.0), 1.0)
    except (TypeError, ValueError):
        conf = 0.0
    return CouncilDecision(d, mult, stop, conf, str(raw.get("reasoning", "")),
                           str(raw.get("dissent", "")), notes=notes)


class Council:
    def __init__(self, cfg: dict[str, Any], client_factory: Callable[[str], LLMClient] | None = None,
                 now: Callable[[], float] = time.time, settings: dict[str, Any] | None = None,
                 name: str = "default"):
        """settings: 기본 council 설정 대신 쓸 설정 (예: Gemini 비교 위원회, config councils.gemini).
        name: 위원회 이름. 회의 기록에 남고, 하루 소집 한도를 위원회별로 센다."""
        c = settings or cfg["council"]
        self.name = name
        self.cfg = c
        self.usd_krw = float(c.get("usd_krw", 1400))
        self.pricing = c.get("pricing", {})
        self.timeout = float(c.get("timeout_sec", 300))
        self.max_tokens = int(c.get("max_tokens", 16000))
        self.max_per_day = int(c.get("max_meetings_per_day", 6))
        self.day_boundary_sec = int(cfg.get("day_boundary_utc_hour", 0)) * 3600
        self._factory = client_factory or (lambda provider: make_client(provider))
        self._clients: dict[str, LLMClient] = {}
        self.now = now

    # ---- 설정 ----
    def role_setting(self, role: str) -> dict[str, Any]:
        base = {"effort": "high", **self.cfg["default"]}
        return {**base, **(self.cfg.get("roles", {}) or {}).get(role, {})}

    def client(self, provider: str) -> LLMClient:
        if provider not in self._clients:
            self._clients[provider] = self._factory(provider)
        return self._clients[provider]

    def cost_krw(self, r: LLMResponse) -> float:
        p = self.pricing.get(r.model) or self.pricing.get(r.model.split("@")[0])
        if p is None and self.cfg.get("free"):
            return 0.0
        if not p:
            return float("nan")
        usd = r.input_tokens / 1e6 * p[0] + r.output_tokens / 1e6 * p[1]
        return usd * self.usd_krw

    def _call(self, role: str, system: str, user: str, schema: dict[str, Any]) -> LLMResponse:
        s = self.role_setting(role)
        try:
            return self.client(s["provider"]).complete(
                model=s["model"], system=system, user=user, schema=schema,
                effort=s["effort"], timeout=self.timeout, max_tokens=self.max_tokens)
        except LLMError as primary:
            fb = self.cfg.get("fallback") or {}
            if not fb.get("enabled"):
                raise
            try:
                r = self.client(fb["provider"]).complete(
                    model=fb["model"], system=system, user=user, schema=schema,
                    effort=s["effort"], timeout=self.timeout, max_tokens=self.max_tokens)
            except LLMError as e:
                raise LLMError(f"{primary} / 예비 제공자도 실패: {e}") from e
            r.used_fallback = True
            r.extra["primary_error"] = str(primary)
            return r

    def meetings_today(self, conn: sqlite3.Connection, run_id: str) -> int:
        day_start = int((self.now() - self.day_boundary_sec) // 86400 * 86400 + self.day_boundary_sec)
        start = datetime.fromtimestamp(day_start, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        return conn.execute(
            """SELECT COUNT(*) FROM council_meetings WHERE run_id=? AND created_at >= ?
               AND error IS NOT 'daily_limit' AND COALESCE(council, 'default') = ?""",
            (run_id, start, self.name)).fetchone()[0]

    # ---- 회의 ----
    def decide(self, conn: sqlite3.Connection, run_id: str, track: str, signal_id: int, bar_ts: int,
               context: dict[str, Any], rule_stop: float | None, price: float) -> CouncilDecision:
        transcript: dict[str, Any] = {}
        providers: dict[str, Any] = {}
        tokens_in = tokens_out = 0
        cost = 0.0
        used_fb = False
        t0 = time.monotonic()
        error: str | None = None
        decision: CouncilDecision | None = None
        chair_raw: dict[str, Any] | None = None

        if self.meetings_today(conn, run_id) >= self.max_per_day:
            error = "daily_limit"
        else:
            try:
                with ThreadPoolExecutor(max_workers=len(MEMBERS)) as ex:
                    futs = {role: ex.submit(self._call, role, *prompts.member_prompt(role, context),
                                            prompts.MEMBER_SCHEMA) for role in MEMBERS}
                    results = {role: f.result() for role, f in futs.items()}
                results["chair"] = self._call("chair", *prompts.chair_prompt(
                    context, {k: v.data for k, v in results.items()}), prompts.CHAIR_SCHEMA)
                for role, r in results.items():
                    transcript[role] = r.data
                    providers[role] = {"provider": r.provider, "model": r.model,
                                       "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                                       "latency_sec": round(r.latency_sec, 2), "used_fallback": r.used_fallback,
                                       **r.extra}
                    tokens_in += r.input_tokens
                    tokens_out += r.output_tokens
                    cost += self.cost_krw(r)
                    used_fb = used_fb or r.used_fallback
                chair_raw = results["chair"].data
                decision = validate_decision(chair_raw, rule_stop, price)
            except LLMError as e:
                error = str(e)
            except Exception as e:  # 예상 못한 오류도 skip
                error = f"{type(e).__name__}: {e}"

        if decision is None:
            decision = CouncilDecision("skip", 0.0, None, 0.0, "", "",
                                       error="하루 최대 소집 횟수 초과" if error == "daily_limit" else error)
        settings = {r: self.role_setting(r) for r in (*MEMBERS, "chair")}
        cur = conn.execute(
            """INSERT INTO council_meetings (created_at, council, run_id, track, signal_id, bar_ts, input, transcript,
                   decision, providers, used_fallback, prompt_version, gen_settings, input_tokens,
                   output_tokens, cost_krw, latency_sec, error)
               VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (self.name, run_id, track, signal_id, bar_ts, json.dumps(context, ensure_ascii=False),
             json.dumps(transcript, ensure_ascii=False),
             json.dumps({"raw": chair_raw, "applied": {
                 "decision": decision.decision, "size_multiplier": decision.size_multiplier,
                 "stop_loss": decision.stop_loss, "confidence": decision.confidence,
                 "notes": decision.notes}}, ensure_ascii=False),
             json.dumps(providers, ensure_ascii=False), int(used_fb), prompts.PROMPT_VERSION,
             json.dumps(settings, ensure_ascii=False), tokens_in, tokens_out,
             cost if transcript else 0.0, round(time.monotonic() - t0, 2), error),
        )
        decision.meeting_id = int(cur.lastrowid)
        return decision
