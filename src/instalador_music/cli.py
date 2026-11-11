"""Modo terminal: `uv run musica <URL> ...` (mesmo motor da interface web)."""

from __future__ import annotations

import argparse
import sys
import time

from . import config, drives
from .downloader import FINISHED, DownloadManager, Job, Options
from .library import Library
from .links import clean_batch

ICONS = {"done": "✅", "skipped": "⏭️ ", "error": "❌", "canceled": "🚫"}


def _bar(p: float, width: int = 28) -> str:
    filled = int(width * p / 100)
    return "█" * filled + "░" * (width - filled)


def _print(job: Job) -> None:
    name = (job.title or job.url)[:48]
    if job.status in FINISHED:
        print(f"\r\033[K{ICONS[job.status]} {name} — {job.message}", flush=True)
    else:
        extra = f" {job.speed} ETA {job.eta}" if job.speed else ""
        print(f"\r\033[K   [{_bar(job.progress)}] {job.progress:5.1f}% {name}{extra}",
              end="", flush=True)


def main() -> None:
    settings = config.load_settings()
    parser = argparse.ArgumentParser(description="Baixa músicas do YouTube em MP3.")
    parser.add_argument("urls", nargs="*", help="links (vídeos ou playlists)")
    parser.add_argument("-o", "--output", default=settings["destination"], help="pasta de destino")
    parser.add_argument("-q", "--quality", default=settings["quality"], choices=config.QUALITIES)
    parser.add_argument("-p", "--playlist", action="store_true", help="permite baixar playlists inteiras")
    parser.add_argument("--force", action="store_true", help="baixa mesmo se já existir")
    parser.add_argument("--keep-names", action="store_true", help="não limpa os nomes dos arquivos")
    args = parser.parse_args()

    text = " ".join(args.urls)
    if not text:
        print("Cole os links (linha vazia para começar):")
        lines = []
        for line in sys.stdin:
            if not line.strip():
                break
            lines.append(line)
        text = "\n".join(lines)

    links = clean_batch(text)
    if not links:
        print("Nenhum link encontrado.")
        return
    for li in links:
        if li.notes:
            print(f"🔗 {li.url or li.original}: {'; '.join(li.notes)}")

    try:
        dest = drives.resolve_destination(args.output)
    except drives.PathNotAllowed as e:
        sys.exit(f"❌ {e}")
    print(f"📁 Destino: {dest}  |  🎚️  {args.quality} kbps\n")

    manager = DownloadManager(Library(), on_update=_print)
    manager.add(links, Options(
        destination=str(dest), quality=args.quality, clean_names=not args.keep_names,
        skip_existing=True, allow_playlists=args.playlist, force=args.force,
    ))
    manager.start()
    try:
        while not manager.idle():
            time.sleep(0.3)
    except KeyboardInterrupt:
        for j in manager.snapshot():
            manager.cancel(j["id"])
        print("\nCancelado.")
        return
    jobs = manager.snapshot()
    ok = sum(j["status"] == "done" for j in jobs)
    print(f"\n🎉 Finalizado: {ok} baixada(s) de {len(jobs)}.")


if __name__ == "__main__":
    main()
