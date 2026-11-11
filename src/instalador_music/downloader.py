"""Fila de downloads em segundo plano, com progresso por música."""

from __future__ import annotations

import itertools
import shutil
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadCancelled

from .library import Library, clean_title, safe_filename, set_title_tag
from .links import LinkInfo

MIN_FREE_BYTES = 50 * 1024 * 1024  # 50 MB de folga
LONG_VIDEO_SECONDS = 15 * 60
_ids = itertools.count(1)

ACTIVE = ("fetching", "downloading", "converting")
FINISHED = ("done", "skipped", "error", "canceled")

POSTPROCESSOR_STEPS = {
    "FFmpegExtractAudio": (88, "Convertendo para MP3…"),
    "FFmpegMetadata": (94, "Gravando título e artista…"),
    "EmbedThumbnail": (97, "Inserindo capa…"),
}


@dataclass
class Options:
    destination: str
    quality: str = "192"
    clean_names: bool = True
    skip_existing: bool = True
    allow_playlists: bool = False
    force: bool = False


@dataclass
class Job:
    url: str
    original: str
    kind: str
    options: Options
    id: int = field(default_factory=lambda: next(_ids))
    video_id: str | None = None
    title: str = ""
    duration: float = 0
    thumbnail: str = ""
    status: str = "queued"
    progress: float = 0.0
    speed: str = ""
    eta: str = ""
    message: str = "Na fila"
    notes: list[str] = field(default_factory=list)
    file: str = ""
    created: float = field(default_factory=time.time)
    finished: float = 0
    cancel_requested: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["destination"] = self.options.destination
        d["quality"] = self.options.quality
        d.pop("options")
        d.pop("cancel_requested")
        return d


class _Logger:
    """Encaminha avisos do yt-dlp para a mensagem do job (sem poluir o terminal)."""

    def __init__(self, job: Job) -> None:
        self.job = job
        self.last_error = ""

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        self.last_error = msg.removeprefix("ERROR: ").strip()


def _fmt_speed(bps: float | None) -> str:
    if not bps:
        return ""
    for unit in ("B/s", "KB/s", "MB/s"):
        if bps < 1024:
            return f"{bps:.0f} {unit}"
        bps /= 1024
    return f"{bps:.1f} GB/s"


def _fmt_eta(sec: float | None) -> str:
    if sec is None:
        return ""
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def _friendly_error(msg: str) -> str:
    low = msg.lower()
    if "sign in to confirm" in low or "429" in low:
        return "YouTube bloqueou temporariamente (muitas requisições). Aguarde alguns minutos."
    if "private video" in low:
        return "Vídeo privado."
    if "video unavailable" in low or "not available" in low:
        return "Vídeo indisponível ou removido."
    if "age" in low and "confirm" in low:
        return "Vídeo com restrição de idade (exige login)."
    if "ffmpeg" in low and ("not found" in low or "not installed" in low):
        return "ffmpeg não encontrado. Instale com: sudo apt install ffmpeg"
    if "no space" in low:
        return "Sem espaço no destino."
    return msg[:300] or "Erro desconhecido"


class DownloadManager:
    def __init__(self, library: Library, on_update: Callable[[Job], None] | None = None) -> None:
        self.library = library
        self.on_update = on_update
        self.jobs: dict[int, Job] = {}
        self._queue: list[int] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # ── API pública ──────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._worker, daemon=True, name="downloader")
            self._thread.start()

    def add(self, links: list[LinkInfo], options: Options) -> list[Job]:
        created = []
        with self._lock:
            queued_keys = {
                (j.video_id or j.url, j.options.destination)
                for j in self.jobs.values()
                if j.status in ("queued", *ACTIVE)
            }
            for link in links:
                if link.duplicate or not link.url:
                    continue
                key = (link.video_id or link.url, options.destination)
                if key in queued_keys:
                    continue
                queued_keys.add(key)
                job = Job(url=link.url, original=link.original, kind=link.kind,
                          options=options, video_id=link.video_id, notes=list(link.notes))
                if link.kind == "playlist" and not options.allow_playlists:
                    job.status = "skipped"
                    job.message = "Playlist ignorada (ative “permitir playlists” para baixar todas)"
                    job.finished = time.time()
                self.jobs[job.id] = job
                if job.status == "queued":
                    self._queue.append(job.id)
                created.append(job)
        self._wake.set()
        return created

    def cancel(self, job_id: int) -> bool:
        with self._lock:
            job = self.jobs.get(job_id)
            if not job or job.status in FINISHED:
                return False
            job.cancel_requested = True
            if job.status == "queued":
                self._queue.remove(job_id)
                self._finish(job, "canceled", "Cancelado")
        return True

    def retry(self, job_id: int, force: bool = False) -> Job | None:
        with self._lock:
            old = self.jobs.get(job_id)
            if not old or old.status not in FINISHED:
                return None
            opts = Options(**{**asdict(old.options), "force": force or old.options.force})
            job = Job(url=old.url, original=old.original, kind=old.kind, options=opts,
                      video_id=old.video_id, notes=old.notes)
            del self.jobs[job_id]
            self.jobs[job.id] = job
            self._queue.append(job.id)
        self._wake.set()
        return job

    def clear_finished(self) -> None:
        with self._lock:
            for jid in [j.id for j in self.jobs.values() if j.status in FINISHED]:
                del self.jobs[jid]

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [j.to_dict() for j in sorted(self.jobs.values(), key=lambda j: j.id)]

    def idle(self) -> bool:
        with self._lock:
            return not self._queue and all(j.status in FINISHED for j in self.jobs.values())

    # ── Internos ─────────────────────────────────────────────────
    def _notify(self, job: Job) -> None:
        if self.on_update:
            try:
                self.on_update(job)
            except Exception:  # noqa: BLE001,S110
                pass

    def _set(self, job: Job, **kw) -> None:
        for k, v in kw.items():
            setattr(job, k, v)
        self._notify(job)

    def _finish(self, job: Job, status: str, message: str) -> None:
        job.status = status
        job.message = message
        job.speed = job.eta = ""
        job.finished = time.time()
        if status == "done":
            job.progress = 100
        self._notify(job)

    def _worker(self) -> None:
        while True:
            self._wake.wait()
            with self._lock:
                job_id = self._queue.pop(0) if self._queue else None
                if job_id is None:
                    self._wake.clear()
                    continue
                job = self.jobs.get(job_id)
            if job:
                self._run(job)
                # Pequena pausa entre músicas para não irritar o YouTube.
                time.sleep(1.5)

    def _base_opts(self, job: Job, logger: _Logger) -> dict:
        return {
            "format": "bestaudio[protocol^=m3u8]/bestaudio/best",
            "js_runtimes": {"deno": {}, "node": {}},
            "noplaylist": True,
            "windowsfilenames": True,  # nomes válidos em pendrive FAT32/Windows
            "retries": 5,
            "fragment_retries": 10,
            "logger": logger,
            "quiet": True,
            "noprogress": True,
            "no_warnings": True,
            "check_formats": False,
        }

    def _run(self, job: Job) -> None:
        if job.cancel_requested:
            return self._finish(job, "canceled", "Cancelado")
        if job.kind == "playlist":
            return self._expand_playlist(job)

        logger = _Logger(job)
        dest = Path(job.options.destination)
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return self._finish(job, "error", f"Não foi possível criar a pasta: {e}")

        self._set(job, status="fetching", progress=2, message="Buscando informações…")
        opts = self._base_opts(job, logger)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(job.url, download=False)
        except DownloadCancelled:
            return self._finish(job, "canceled", "Cancelado")
        except Exception as e:  # noqa: BLE001
            return self._finish(job, "error", _friendly_error(logger.last_error or str(e)))
        if not info:
            return self._finish(job, "error", _friendly_error(logger.last_error))

        job.video_id = info.get("id") or job.video_id
        title = info.get("track") and info.get("artist") and f"{info['artist']} - {info['track']}"
        job.title = title or info.get("title") or job.url
        job.duration = info.get("duration") or 0
        job.thumbnail = info.get("thumbnail") or ""
        job.title = unicodedata.normalize("NFKC", job.title)
        if job.duration > LONG_VIDEO_SECONDS:
            job.notes.append(f"Vídeo longo ({int(job.duration // 60)} min): pode ser uma compilação")
        self._set(job, progress=5, message="Verificando biblioteca…")

        if job.options.skip_existing and not job.options.force:
            existing = self.library.find_existing(dest, job.video_id, info.get("title") or "")
            if existing:
                same = existing.video_id and existing.video_id == job.video_id
                why = "Já existe" if same else "Provável duplicata"
                job.file = existing.path
                return self._finish(job, "skipped", f"{why}: {Path(existing.path).name}")
        self._set(job, progress=6, message="Preparando download…")

        try:
            if shutil.disk_usage(dest).free < MIN_FREE_BYTES:
                return self._finish(job, "error", "Pouco espaço livre no destino (< 50 MB).")
        except OSError:
            pass

        final_path: dict[str, str] = {}

        def progress_hook(d: dict) -> None:
            if job.cancel_requested:
                raise DownloadCancelled()
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                if total:
                    pct = 5 + 80 * min(done / total, 1)
                elif d.get("fragment_count"):
                    pct = 5 + 80 * (d.get("fragment_index", 0) / d["fragment_count"])
                else:
                    pct = job.progress
                # A estimativa de tamanho do HLS oscila: nunca deixa a barra voltar.
                self._set(job, status="downloading", progress=round(max(pct, job.progress), 1),
                          speed=_fmt_speed(d.get("speed")), eta=_fmt_eta(d.get("eta")),
                          message="Baixando áudio…")
            elif d["status"] == "finished":
                self._set(job, status="converting", progress=86, speed="", eta="",
                          message="Download concluído, convertendo…")

        def pp_hook(d: dict) -> None:
            if job.cancel_requested:
                raise DownloadCancelled()
            step = POSTPROCESSOR_STEPS.get(d.get("postprocessor", ""))
            if d["status"] == "started" and step:
                self._set(job, status="converting", progress=step[0], message=step[1])
            if d["status"] == "finished":
                fp = (d.get("info_dict") or {}).get("filepath")
                if fp:
                    final_path["path"] = fp

        opts.update({
            "outtmpl": str(dest / "%(title)s.%(ext)s"),
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                 "preferredquality": job.options.quality},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ],
            "writethumbnail": True,
            "keepvideo": False,
            "overwrites": False,
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [pp_hook],
        })
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                result = ydl.process_ie_result(info, download=True)
        except DownloadCancelled:
            return self._finish(job, "canceled", "Cancelado")
        except Exception as e:  # noqa: BLE001
            return self._finish(job, "error", _friendly_error(logger.last_error or str(e)))

        path = final_path.get("path")
        if not path and result:
            reqs = result.get("requested_downloads") or [{}]
            path = reqs[0].get("filepath")
        if not path or not Path(path).exists():
            return self._finish(job, "error", _friendly_error(logger.last_error or "Arquivo final não encontrado"))

        file = Path(path)
        if job.options.clean_names:
            file = self._clean_name(file)
        job.file = str(file)
        self._finish(job, "done", f"Salvo em {file.name}")

    def _clean_name(self, file: Path) -> Path:
        stem = safe_filename(clean_title(file.stem))
        target = file.with_name(stem + file.suffix)
        if target != file and not target.exists():
            try:
                file.rename(target)
                file = target
            except OSError:
                return file
        set_title_tag(file, file.stem)
        return file

    def _expand_playlist(self, job: Job) -> None:
        self._set(job, status="fetching", progress=10, message="Listando playlist…")
        logger = _Logger(job)
        opts = {**self._base_opts(job, logger), "extract_flat": "in_playlist",
                "noplaylist": False, "playlistend": 300}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(job.url, download=False)
        except Exception as e:  # noqa: BLE001
            return self._finish(job, "error", _friendly_error(logger.last_error or str(e)))
        entries = [e for e in (info or {}).get("entries") or [] if e and e.get("id")]
        job.title = (info or {}).get("title") or "Playlist"
        links = [
            LinkInfo(original=job.url, url=f"https://www.youtube.com/watch?v={e['id']}",
                     kind="video", video_id=e["id"], notes=[f"Da playlist: {job.title}"])
            for e in entries
        ]
        added = self.add(links, job.options)
        self._finish(job, "done", f"{len(added)} músicas adicionadas à fila")
