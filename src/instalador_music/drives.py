"""Informações sobre pastas de destino, discos montados e pendrives (inclui WSL)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from functools import cache
from pathlib import Path

from .library import AUDIO_EXTS


# Pastas onde o app aceita salvar/limpar músicas. Evita que a API mexa em
# diretórios do sistema caso algo malicioso tente chamá-la.
def allowed_roots() -> list[Path]:
    roots = [Path.home(), Path("/mnt"), Path("/media"), Path("/run/media"), Path.cwd()]
    return [r.resolve() for r in roots if r.exists()]


class PathNotAllowed(ValueError):
    pass


def resolve_destination(raw: str) -> Path:
    """Valida e normaliza uma pasta de destino informada pelo usuário."""
    raw = (raw or "").strip()
    if not raw:
        raise PathNotAllowed("Informe uma pasta de destino.")
    # Aceita caminho do Windows colado (D:\\playlist) e converte para WSL.
    if is_wsl() and len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        raw = f"/mnt/{raw[0].lower()}/" + raw[2:].replace("\\", "/").lstrip("/")
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not any(path == r or r in path.parents for r in allowed_roots()):
        raise PathNotAllowed(
            f"Pasta não permitida: {path}. Use uma pasta dentro de {Path.home()}, /mnt ou /media."
        )
    if path.exists() and not path.is_dir():
        raise PathNotAllowed(f"{path} é um arquivo, não uma pasta.")
    return path


@cache
def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def windows_path(path: Path) -> str | None:
    parts = path.parts
    if is_wsl() and len(parts) >= 3 and parts[1] == "mnt" and len(parts[2]) == 1:
        rest = "\\".join(parts[3:])
        return f"{parts[2].upper()}:\\{rest}"
    return None


def path_info(raw: str) -> dict:
    try:
        path = resolve_destination(raw)
    except PathNotAllowed as e:
        return {"ok": False, "error": str(e)}
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    try:
        usage = shutil.disk_usage(existing)
        free, total = usage.free, usage.total
    except OSError:
        free = total = 0
    count = 0
    if path.is_dir():
        try:
            count = sum(1 for p in path.iterdir() if p.suffix.lower() in AUDIO_EXTS)
        except OSError:
            pass
    return {
        "ok": True,
        "path": str(path),
        "windows_path": windows_path(path),
        "exists": path.is_dir(),
        "writable": os.access(existing, os.W_OK),
        "free": free,
        "total": total,
        "tracks": count,
    }


def mounted_drives() -> list[dict]:
    drives = []
    try:
        lines = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return drives
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        mnt, fstype = fields[1].replace("\\040", " "), fields[2]
        if not (mnt.startswith(("/mnt/", "/media/", "/run/media/"))):
            continue
        if mnt.startswith(("/mnt/wsl", "/mnt/wslg")):
            continue
        try:
            usage = shutil.disk_usage(mnt)
        except OSError:
            continue
        drives.append({
            "path": mnt, "fstype": fstype, "free": usage.free, "total": usage.total,
            "windows_path": windows_path(Path(mnt)),
        })
    return drives


def windows_removable() -> list[dict]:
    """No WSL, lista pendrives do Windows e diz se já estão montados no Linux."""
    if not is_wsl() or not shutil.which("powershell.exe"):
        return []
    cmd = (
        "Get-Volume | Where-Object { $_.DriveType -eq 'Removable' -and $_.DriveLetter } | "
        "Select-Object DriveLetter,FileSystemLabel,FileSystem,SizeRemaining,Size | "
        "ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout.strip()
        data = json.loads(out) if out else []
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    if isinstance(data, dict):
        data = [data]
    mounted = {d["path"] for d in mounted_drives()}
    result = []
    for v in data:
        letter = str(v.get("DriveLetter") or "").lower()
        if not letter:
            continue
        mnt = f"/mnt/{letter}"
        result.append({
            "letter": letter.upper(),
            "label": v.get("FileSystemLabel") or "",
            "fstype": v.get("FileSystem") or "",
            "free": v.get("SizeRemaining") or 0,
            "total": v.get("Size") or 0,
            "mount_path": mnt,
            "mounted": mnt in mounted,
            "mount_cmd": f"sudo mkdir -p {mnt} && sudo mount -t drvfs {letter.upper()}: {mnt}",
        })
    return result
