"""自动更新检测：检查 GitHub Releases 是否有新版本，并支持下载。"""
from __future__ import annotations

import json
import threading
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import __github_repo__, __version__

API_URL = f"https://api.github.com/repos/{__github_repo__}/releases/latest"


@dataclass
class ReleaseInfo:
    tag_name: str = ""
    name: str = ""
    html_url: str = ""
    body: str = ""
    assets: list = field(default_factory=list)


def _parse_version(tag: str) -> tuple:
    tag = tag.lstrip("vV")
    parts = []
    for p in tag.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_newer(latest_tag: str, current: str) -> bool:
    return _parse_version(latest_tag) > _parse_version(current)


def fetch_latest(timeout: int = 12) -> Optional[ReleaseInfo]:
    try:
        req = urllib.request.Request(
            API_URL,
            headers={"User-Agent": "MChat-Updater", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return ReleaseInfo(
            tag_name=data.get("tag_name", ""),
            name=data.get("name", ""),
            html_url=data.get("html_url", ""),
            body=data.get("body", ""),
            assets=data.get("assets", []),
        )
    except Exception:  # noqa: BLE001
        return None


def check_update_async(callback: Callable[[Optional[ReleaseInfo]], None]) -> None:
    """在后台线程检查更新，完成后在后台线程回调。"""
    def worker() -> None:
        callback(fetch_latest())
    threading.Thread(target=worker, daemon=True).start()


def download_asset(
    url: str,
    dest: str,
    progress: Optional[Callable[[int, int], None]] = None,
) -> None:
    """下载更新包到本地，可选进度回调 (done_bytes, total_bytes)。"""
    req = urllib.request.Request(url, headers={"User-Agent": "MChat-Updater"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length", 0) or 0)
        done = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
