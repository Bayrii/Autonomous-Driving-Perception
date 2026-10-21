from __future__ import annotations

import copy
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "configs" / "default.toml"
LOCAL_CONFIG = ROOT / "configs" / "local.toml"  # optional, machine-specific, not in git


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: str | Path | None = None) -> dict:
    """configs/default.toml, then configs/local.toml if present, then ``path``."""
    cfg = tomllib.loads(DEFAULT_CONFIG.read_text())
    if LOCAL_CONFIG.exists():
        cfg = _merge(cfg, tomllib.loads(LOCAL_CONFIG.read_text()))
    if path:
        cfg = _merge(cfg, tomllib.loads(Path(path).read_text()))
    return cfg


def resolve(p: str | Path) -> Path:
    """Relative config paths are relative to the repository root."""
    p = Path(p)
    return p if p.is_absolute() else ROOT / p
