"""Configurações persistentes do instalador (JSON na pasta de dados do usuário)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

QUALITIES = ("128", "192", "256", "320")
_lock = threading.Lock()


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    d = Path(base) / "instalador-music"
    d.mkdir(parents=True, exist_ok=True)
    return d


def trash_dir() -> Path:
    d = data_dir() / "lixeira"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _settings_file() -> Path:
    return data_dir() / "settings.json"


def default_settings() -> dict:
    default_dest = str((Path.cwd() / "musicas").resolve())
    return {
        "destination": default_dest,
        "destinations": [{"name": "Músicas (projeto)", "path": default_dest}],
        "quality": "192",
        "clean_names": True,
        "skip_existing": True,
        "allow_playlists": False,
    }


def load_settings() -> dict:
    settings = default_settings()
    with _lock:
        try:
            saved = json.loads(_settings_file().read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                settings.update({k: v for k, v in saved.items() if k in settings})
        except (OSError, ValueError):
            pass
    return settings


def save_settings(new: dict) -> dict:
    settings = load_settings()
    for key in settings:
        if key in new:
            settings[key] = new[key]
    if settings["quality"] not in QUALITIES:
        settings["quality"] = "192"
    for key in ("clean_names", "skip_existing", "allow_playlists"):
        settings[key] = bool(settings[key])
    dests = []
    seen = set()
    for d in settings.get("destinations") or []:
        if not isinstance(d, dict) or not d.get("path"):
            continue
        path = str(d["path"]).strip()
        if path in seen:
            continue
        seen.add(path)
        dests.append({"name": str(d.get("name") or Path(path).name)[:60], "path": path})
    settings["destinations"] = dests
    with _lock:
        tmp = _settings_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_settings_file())
    return settings
