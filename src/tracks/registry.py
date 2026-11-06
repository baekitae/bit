"""config.yaml 의 트랙 정의로 트랙 객체를 만든다."""
from __future__ import annotations

import sqlite3
from typing import Any

from src.strategies import make_strategy
from src.tracks.base import Track
from src.tracks.tracks import CouncilTrack, RuleTrack


def track_defs(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """트랙 이름 → 정의. B 트랙은 짝 A 트랙의 전략·파라미터·봉 주기를 물려받는다."""
    t = cfg["tracks"]
    raw = dict(t.get("defs") or {})
    s = cfg.get("strategy") or {}
    legacy = {"type": "rule", "strategy": s.get("name"), "params": s.get("params", {}),
              "version": s.get("version", "v1"), "timeframe": cfg["timeframe_minutes"]}
    out: dict[str, dict[str, Any]] = {}
    for name in t["enabled"]:
        d = dict(raw.get(name) or legacy)
        if d.get("type") == "council":
            base = dict(raw[d["mirror"]]) if d.get("mirror") in raw else dict(legacy)
            d = {**base, **d, "type": "council"}
        d.setdefault("version", "v1")
        d.setdefault("timeframe", cfg["timeframe_minutes"])
        d.setdefault("params", {})
        out[name] = d
    return out


def build_tracks(conn: sqlite3.Connection, cfg: dict[str, Any], run_id: str,
                 names: list[str] | None = None, council: Any = None) -> list[Track]:
    defs = track_defs(cfg)
    out: list[Track] = []
    for name in names or list(defs):
        d = defs[name]
        strategy = make_strategy(d["strategy"], d["params"])
        common = dict(name=name, unit=int(d["timeframe"]))
        if d["type"] == "rule":
            out.append(RuleTrack(conn, run_id, cfg, strategy, d["version"], **common))
        elif d["type"] == "council":
            if council is None:
                raise ValueError(f"{name}: AI 위원회 트랙에는 council 이 필요합니다")
            out.append(CouncilTrack(conn, run_id, cfg=cfg, strategy=strategy, strategy_version=d["version"],
                                    council=council, mirror=d.get("mirror"), **common))
        else:
            raise ValueError(f"{name}: 알 수 없는 트랙 종류 {d['type']}")
    # A(규칙) 트랙을 먼저 처리해야 B 트랙이 같은 봉의 A 신호와 짝을 지을 수 있다
    out.sort(key=lambda t: 0 if isinstance(t, RuleTrack) else 1)
    return out
