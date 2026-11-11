"""Servidor web local do instalador (FastAPI)."""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import threading
import time
import unicodedata
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import version as pkg_version
from importlib.resources import files
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import __version__, config, drives
from .downloader import DownloadManager, Options
from .library import (
    Library,
    apply_renames,
    find_duplicates,
    find_junk,
    move_to_trash,
    rename_plan,
    title_key,
)
from .links import VIDEO_ID_RE, clean_batch

TOKEN = secrets.token_urlsafe(32)
library = Library()
manager = DownloadManager(library)
app = FastAPI(title="Instalador de Músicas", docs_url=None, redoc_url=None, openapi_url=None)
_allowed_hosts: set[str] = set()


# ── Segurança ───────────────────────────────────────────────────
@app.middleware("http")
async def guard(request: Request, call_next):
    # Bloqueia DNS rebinding: só aceita o Host do próprio servidor local.
    if _allowed_hosts and request.headers.get("host", "") not in _allowed_hosts:
        return JSONResponse({"detail": "Host não permitido"}, status_code=403)
    if request.url.path.startswith("/api/"):
        # Token aleatório por execução, só presente na página servida: impede
        # que outro site aberto no navegador dispare ações (CSRF).
        if not secrets.compare_digest(request.headers.get("x-token", ""), TOKEN):
            return JSONResponse({"detail": "Token inválido"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' https://i.ytimg.com data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; frame-ancestors 'none'"
    )
    return response


def _dest(raw: str) -> Path:
    try:
        return drives.resolve_destination(raw)
    except drives.PathNotAllowed as e:
        raise HTTPException(400, str(e)) from e


# ── Modelos ─────────────────────────────────────────────────────
class TextIn(BaseModel):
    text: str = Field(max_length=200_000)


class JobsIn(TextIn):
    destination: str
    quality: str = "192"
    clean_names: bool = True
    skip_existing: bool = True
    allow_playlists: bool = False


class FolderIn(BaseModel):
    path: str


class FilesIn(FolderIn):
    files: list[str] = Field(max_length=5000)


class RenameItem(BaseModel):
    from_: str = Field(alias="from")
    new_name: str = Field(max_length=200)


class RenamesIn(FolderIn):
    renames: list[RenameItem] = Field(max_length=5000)


class SettingsIn(BaseModel):
    destination: str | None = None
    destinations: list[dict] | None = None
    quality: str | None = None
    clean_names: bool | None = None
    skip_existing: bool | None = None
    allow_playlists: bool | None = None


# ── Páginas ─────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index() -> str:
    html = files("instalador_music").joinpath("static/index.html").read_text(encoding="utf-8")
    return html.replace("__TOKEN__", TOKEN).replace("__VERSION__", __version__)


# ── Downloads ───────────────────────────────────────────────────
@app.get("/api/state")
def state() -> dict:
    return {"jobs": manager.snapshot(), "settings": config.load_settings()}


@app.post("/api/links/analyze")
def analyze_links(body: TextIn) -> list[dict]:
    return [li.to_dict() for li in clean_batch(body.text)]


class PreviewIn(BaseModel):
    ids: list[str] = Field(max_length=50)
    destination: str = ""


_preview_cache: dict[str, dict] = {}


def _oembed(video_id: str) -> dict:
    """Título e canal via oEmbed (leve, sem baixar a página nem usar o yt-dlp)."""
    if video_id in _preview_cache:
        return _preview_cache[video_id]
    data = {"title": "", "author": ""}
    url = f"https://www.youtube.com/oembed?format=json&url=https://www.youtube.com/watch?v={video_id}"
    try:
        with urllib.request.urlopen(url, timeout=6) as r:  # noqa: S310 - URL fixa https
            j = json.load(r)
            data = {"title": j.get("title", ""), "author": j.get("author_name", "")}
            _preview_cache[video_id] = data
    except Exception:  # noqa: BLE001
        data["unavailable"] = True
    return data


@app.post("/api/links/preview")
def preview_links(body: PreviewIn) -> dict:
    ids = [i for i in dict.fromkeys(body.ids) if VIDEO_ID_RE.match(i)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        meta = dict(zip(ids, pool.map(_oembed, ids), strict=True))
    tracks = []
    if body.destination:
        try:
            tracks = library.scan(drives.resolve_destination(body.destination))
        except drives.PathNotAllowed:
            pass
    by_id = {t.video_id: t for t in tracks if t.video_id}
    by_key = {t.key: t for t in tracks if t.key}
    out = {}
    for vid in ids:
        m = dict(meta[vid])
        m["title"] = unicodedata.normalize("NFKC", m.get("title", ""))
        hit = by_id.get(vid) or (by_key.get(title_key(m["title"])) if m["title"] else None)
        m["existing"] = Path(hit.path).name if hit else None
        m["existing_exact"] = bool(hit and hit.video_id == vid)
        out[vid] = m
    return out


@app.post("/api/jobs")
def add_jobs(body: JobsIn) -> dict:
    if body.quality not in config.QUALITIES:
        raise HTTPException(400, "Qualidade inválida")
    dest = _dest(body.destination)
    links = clean_batch(body.text)
    if not links:
        raise HTTPException(400, "Nenhum link encontrado no texto.")
    opts = Options(
        destination=str(dest), quality=body.quality, clean_names=body.clean_names,
        skip_existing=body.skip_existing, allow_playlists=body.allow_playlists,
    )
    jobs = manager.add(links, opts)
    config.save_settings({
        "destination": str(dest), "quality": body.quality, "clean_names": body.clean_names,
        "skip_existing": body.skip_existing, "allow_playlists": body.allow_playlists,
    })
    return {"added": len(jobs), "ignored": len(links) - len(jobs)}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int) -> dict:
    return {"ok": manager.cancel(job_id)}


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: int, force: bool = False) -> dict:
    job = manager.retry(job_id, force=force)
    if not job:
        raise HTTPException(404, "Job não encontrado ou ainda em andamento")
    return {"ok": True, "id": job.id}


@app.post("/api/jobs/clear")
def clear_jobs() -> dict:
    manager.clear_finished()
    return {"ok": True}


# ── Destinos / discos ───────────────────────────────────────────
@app.get("/api/destination")
def destination_info(path: str) -> dict:
    return drives.path_info(path)


@app.get("/api/drives")
def list_drives() -> dict:
    return {
        "wsl": drives.is_wsl(),
        "mounted": drives.mounted_drives(),
        "removable": drives.windows_removable(),
        "home": str(Path.home()),
    }


@app.post("/api/folder/open")
def open_folder(body: FolderIn) -> dict:
    """Abre a pasta no gerenciador de arquivos (Explorer no WSL)."""
    dest = _dest(body.path)
    if not dest.is_dir():
        raise HTTPException(404, "A pasta ainda não existe (será criada no primeiro download).")
    win = drives.windows_path(dest)
    if drives.is_wsl() and shutil.which("explorer.exe"):
        target = win or subprocess.run(
            ["wslpath", "-w", str(dest)], capture_output=True, text=True, check=False
        ).stdout.strip()
        subprocess.Popen(["explorer.exe", target])  # noqa: S603
    elif shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", str(dest)])  # noqa: S603
    else:
        raise HTTPException(501, "Nenhum gerenciador de arquivos disponível.")
    return {"ok": True}


@app.post("/api/settings")
def update_settings(body: SettingsIn) -> dict:
    data = body.model_dump(exclude_none=True)
    for d in data.get("destinations") or []:
        if d.get("path"):
            d["path"] = str(_dest(str(d["path"])))
    if data.get("destination"):
        data["destination"] = str(_dest(data["destination"]))
    return config.save_settings(data)


# ── Biblioteca ──────────────────────────────────────────────────
@app.get("/api/library")
def library_report(path: str) -> dict:
    dest = _dest(path)
    tracks = library.scan(dest)
    dups = find_duplicates(tracks)
    return {
        "path": str(dest),
        "tracks": [t.to_dict() for t in tracks],
        "total_size": sum(t.size for t in tracks),
        "total_duration": sum(t.duration for t in tracks),
        "duplicates": dups,
        "renames": rename_plan(tracks),
        "junk": find_junk(dest),
        "trash": str(config.trash_dir()),
    }


@app.post("/api/library/trash")
def trash_files(body: FilesIn) -> dict:
    dest = _dest(body.path)
    try:
        moved = move_to_trash(dest, body.files)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"moved": len(moved), "trash": str(config.trash_dir())}


@app.post("/api/library/junk")
def clean_junk(body: FolderIn) -> dict:
    dest = _dest(body.path)
    removed = 0
    for j in find_junk(dest):
        try:
            Path(j["path"]).unlink()
            removed += 1
        except OSError:
            pass
    return {"removed": removed}


@app.post("/api/library/rename")
def rename_files(body: RenamesIn) -> dict:
    dest = _dest(body.path)
    try:
        done = apply_renames(dest, [{"from": r.from_, "new_name": r.new_name} for r in body.renames])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"renamed": len(done)}


# ── Versões / atualizações ──────────────────────────────────────
_latest_cache: dict[str, tuple[float, str | None]] = {}


def _latest(pkg: str) -> str | None:
    hit = _latest_cache.get(pkg)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    try:
        with urllib.request.urlopen(  # noqa: S310 - URL fixa https
            f"https://pypi.org/pypi/{pkg}/json", timeout=5
        ) as r:
            latest = json.load(r)["info"]["version"]
    except Exception:  # noqa: BLE001
        latest = None
    _latest_cache[pkg] = (time.time(), latest)
    return latest


@app.get("/api/versions")
def versions() -> dict:
    out = []
    for pkg in ("yt-dlp", "fastapi", "uvicorn", "mutagen"):
        try:
            cur = pkg_version(pkg)
        except Exception:  # noqa: BLE001
            cur = None
        latest = _latest(pkg)
        out.append({"name": pkg, "installed": cur, "latest": latest,
                    "outdated": bool(cur and latest and cur != latest)})
    ffmpeg = shutil.which("ffmpeg")
    js = shutil.which("deno") or shutil.which("node")
    return {
        "app": __version__,
        "packages": out,
        "ffmpeg": bool(ffmpeg),
        "js_runtime": Path(js).name if js else None,
        "update_cmd": "uv lock --upgrade && uv sync",
        "update_ytdlp_cmd": "uv lock --upgrade-package yt-dlp && uv sync",
    }


# ── Inicialização ───────────────────────────────────────────────
def _open_browser(url: str) -> None:
    time.sleep(1.2)
    if drives.is_wsl():
        for cmd in (["wslview", url], ["cmd.exe", "/c", "start", "", url]):
            if shutil.which(cmd[0]):
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
                return
    webbrowser.open(url)


def _free_port(host: str, start: int) -> int:
    """Usa a porta pedida ou a próxima livre (outro app pode estar nela)."""
    import socket

    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
            except OSError:
                continue
            if port != start:
                print(f"⚠️  Porta {start} ocupada por outro programa, usando {port}.")
            return port
    raise SystemExit(f"Nenhuma porta livre entre {start} e {start + 19}. Use --port.")


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Instalador de músicas — interface web")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="não abre o navegador")
    args = parser.parse_args()

    host = "127.0.0.1"  # nunca expõe na rede
    args.port = _free_port(host, args.port)
    _allowed_hosts.update({f"127.0.0.1:{args.port}", f"localhost:{args.port}"})
    url = f"http://localhost:{args.port}"
    manager.start()
    print(f"🎶 Instalador de Músicas v{__version__} rodando em {url}")
    print("   Ctrl+C para encerrar.")
    if not args.no_browser:
        threading.Thread(target=_open_browser, args=(url,), daemon=True).start()
    uvicorn.run(app, host=host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
