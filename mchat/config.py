"""配置管理：读取/写入 config.json（单账号模型）。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 数据目录：源码运行在项目根，打包后在 %LOCALAPPDATA%/MChat
if getattr(sys, "frozen", False):
    _base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    ROOT = Path(_base) / "MChat"
else:
    ROOT = Path(__file__).resolve().parent.parent

ROOT.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = ROOT / "config.json"

DEFAULT_CONFIG = {
    "homeserver": "https://matrix.org",
    "proxy": "",
    "user_id": "",
    "password": "",
    "access_token": "",
    "device_id": "MChat",
    "store_dir": "store",
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return _merge(DEFAULT_CONFIG, cfg)
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def is_configured(cfg: dict | None = None) -> bool:
    if cfg is None:
        cfg = load_config()
    return bool(cfg.get("user_id") and (cfg.get("password") or cfg.get("access_token")))


def _merge(default: dict, override: dict) -> dict:
    result = dict(default)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result
