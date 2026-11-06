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
        if d.get("type") == "evolving":
            base = dict(raw[d["base"]])
            d = {**base, **d, "type": "evolving"}
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
    """council: Council 하나, 또는 {위원회 이름: Council} (트랙 정의의 council 항목으로 고름, 기본 'default')."""
    defs = track_defs(cfg)
    councils = council if isinstance(council, dict) else ({"default": council} if council is not None else {})
    out: list[Track] = []
    for name in names or list(defs):
        d = defs[name]
        council = councils.get(d.get("council", "default"))
        strategy = make_strategy(d["strategy"], d["params"])
        common = dict(name=name, unit=int(d["timeframe"]))
        if d["type"] == "rule":
            out.append(RuleTrack(conn, run_id, cfg, strategy, d["version"], **common))
        elif d["type"] == "council":
            if council is None:
                raise ValueError(f"{name}: AI 위원회 '{d.get('council', 'default')}' 가 필요합니다")
            out.append(CouncilTrack(conn, run_id, cfg=cfg, strategy=strategy, strategy_version=d["version"],
                                    council=council, mirror=d.get("mirror"), **common))
        elif d["type"] == "evolving":
            if council is None:
                raise ValueError(f"{name}: C 트랙에는 council 이 필요합니다")
            out.extend(_evolving_tracks(conn, cfg, run_id, name, d, council))
        else:
            raise ValueError(f"{name}: 알 수 없는 트랙 종류 {d['type']}")
    # A(규칙) 트랙을 먼저 처리해야 B 트랙이 같은 봉의 A 신호와 짝을 지을 수 있다
    out.sort(key=lambda t: 0 if isinstance(t, RuleTrack) else 1)
    return out


def _evolving_tracks(conn, cfg, run_id, name, d, council) -> list[Track]:
    """C 트랙: 활성 버전 1개 + (있으면) 섀도 버전 1개. 짝비교(mirror)는 하지 않는다 (파라미터가 A 와 달라지므로)."""
    import json

    from src.evolution.manager import get_version, shadow_track_name

    out: list[Track] = []
    unit = int(d["timeframe"])
    act = get_version(conn, run_id, name, "active")
    params = json.loads(act["params"]) if act else d["params"]
    version = act["version"] if act else "v1"
    out.append(CouncilTrack(conn, run_id, cfg=cfg, strategy=make_strategy(d["strategy"], params),
                            strategy_version=version, council=council, mirror=None, name=name, unit=unit))
    sh = get_version(conn, run_id, name, "shadow")
    if sh is not None:
        out.append(CouncilTrack(conn, run_id, cfg=cfg, strategy=make_strategy(d["strategy"], json.loads(sh["params"])),
                                strategy_version=sh["version"], council=council, mirror=None,
                                name=shadow_track_name(name, sh["version"]), unit=unit))
    return out
