"""配置管理：读取/写入 config.json。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 数据目录：
# - 源码运行：项目根目录（方便开发调试）
# - PyInstaller 打包后：%LOCALAPPDATA%/MChat（持久化，避免写入只读的程序目录）
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
    "room_id": "",
    "room_name": "MChat 通道",
    "accounts": {
        "a": {
            "label": "程序A",
            "user_id": "",
            "password": "",
            "access_token": "",
            "device_id": "MChat-A",
            "store_dir": "store_a",
        },
        "b": {
            "label": "程序B",
            "user_id": "",
            "password": "",
            "access_token": "",
            "device_id": "MChat-B",
            "store_dir": "store_b",
        },
    },
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


def account(cfg: dict, key: str) -> dict:
    return cfg["accounts"][key]


def _merge(default: dict, override: dict) -> dict:
    result = dict(default)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result
