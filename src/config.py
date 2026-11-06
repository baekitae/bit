"""설정 로드: config.yaml + .env"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    p = Path(path) if path else ROOT / "config.yaml"
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg


def db_path(cfg: dict[str, Any]) -> Path:
    p = Path(cfg["db_path"])
    return p if p.is_absolute() else ROOT / p
