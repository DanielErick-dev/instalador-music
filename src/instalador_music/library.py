"""Biblioteca de músicas: leitura de tags, nomes limpos, duplicatas e lixo."""

from __future__ import annotations

import hashlib
import re
import shutil
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

import mutagen
from mutagen.id3 import ID3, TIT2, ID3NoHeaderError

from .config import trash_dir

AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav", ".aac"}
# Restos de downloads interrompidos ou de pós-processamento.
JUNK_SUFFIXES = (".part", ".ytdl", ".temp")
SIDECAR_SUFFIXES = (".webp", ".jpg", ".jpeg", ".png", ".webm")
JUNK_PATTERNS = (re.compile(r"\.part-Frag\d+"), re.compile(r"\.temp\.\w+$"), re.compile(r"\.f\d+\.\w+$"))

YT_ID_IN_TEXT = re.compile(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})")

# Caracteres "de largura total" que o yt-dlp usa para nomes válidos no Windows.
FULLWIDTH_MAP = str.maketrans({
    "＂": "'", "｜": "-", "⧸": "-", "／": "-", "＼": "-", "：": " -", "？": "",
    "＊": "", "＜": "", "＞": "", "｀": "'", "＃": "#", "＆": "&",
})

# Caracteres proibidos em nomes de arquivo no Windows/FAT32.
FORBIDDEN_MAP = str.maketrans({'"': "'", "|": "-", "/": "-", "\\": "-", ":": " -", "?": "", "*": "", "<": "", ">": ""})

_NOISE = (
    r"official\s*(?:music\s*)?(?:video|audio|lyric\s*video|visuali[sz]er|version|clip)?"
    r"|(?:music|lyrics?|lyric)\s*video|lyrics?|letra|legendad[oa]|tradu[cç][aã]o|traduzid[oa]"
    r"|clipe\s*oficial|v[ií]deo\s*oficial|[aá]udio\s*oficial|audio|[aá]udio|video|v[ií]deo"
    r"|hd|hq|4k|1080p|720p|visuali[sz]er|m/?v|sub(?:titulado)?\s*(?:espa[nñ]ol|pt|br)?"
    r"|color\s*coded|with\s*lyrics|ao\s*vivo\s*oficial"
)
NOISE_BRACKETS = re.compile(
    rf"\s*[\(\[【]\s*(?:{_NOISE})(?:\s*[-/&+,]\s*(?:{_NOISE}))*\s*[\)\]】]",
    re.IGNORECASE,
)
NOISE_TRAILING = re.compile(
    r"\s*(?:[-|/~]+\s*)?(?:official\s*(?:music\s*)?(?:video|audio|version)"
    r"|sub(?:titulado)?\s*espa[nñ]ol|legendad[oa]|tradu[cç][aã]o|with\s*lyrics|lyrics?)\s*$",
    re.IGNORECASE,
)


def clean_title(name: str) -> str:
    """Remove ruído do título: "(Official Video)", "[Lyric Video]", "⧸⧸ Sub Español"..."""
    # Letras "estilizadas" (𝐌𝐮𝐬𝐢𝐜, 𝟑𝟎) viram texto normal (NFKC), depois do mapa
    # de largura total para não reintroduzir caracteres proibidos (｜ → |).
    text = unicodedata.normalize("NFKC", name.translate(FULLWIDTH_MAP))
    text = text.translate(FORBIDDEN_MAP)
    for _ in range(3):  # pode haver mais de um bloco de ruído
        new = NOISE_BRACKETS.sub("", text)
        new = NOISE_TRAILING.sub("", new)
        if new == text:
            break
        text = new
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"(\s*-\s*){2,}", " - ", text)
    text = text.strip(" -_|.")
    return text or name.strip()


def title_key(name: str) -> str:
    """Chave para comparar títulos (sem acento, caixa ou pontuação)."""
    text = unicodedata.normalize("NFKD", clean_title(name).lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"\b(?:feat|ft|featuring)\b.*$", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def safe_filename(name: str) -> str:
    """Nome válido em FAT32/NTFS (pendrive) e Linux."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(" .")
    return name[:180] or "musica"


@dataclass
class Track:
    path: str
    name: str
    size: int
    mtime: float
    duration: float
    bitrate: int
    title: str
    artist: str
    video_id: str | None
    key: str

    def to_dict(self) -> dict:
        return asdict(self)


def _first_text(tags, *keys) -> str:
    for k in keys:
        try:
            v = tags.get(k)
        except (KeyError, ValueError):
            v = None
        if v:
            text = getattr(v, "text", v)
            if isinstance(text, list):
                text = text[0] if text else ""
            return str(text)
    return ""


def _video_id_from_tags(tags) -> str | None:
    """Lê o ID do vídeo gravado pelo yt-dlp (purl/comment).

    A descrição é ignorada: ela costuma citar *outros* vídeos do YouTube.
    """
    keys = [str(k) for k in tags.keys()]  # noqa: SIM118 - tags do mutagen não são dict
    preferred = [k for k in keys if any(w in k.lower() for w in ("purl", "comment", "comm", "wxxx", "woas"))]
    for k in preferred:
        m = YT_ID_IN_TEXT.search(str(tags[k]))
        if m:
            return m.group(1)
    return None


def read_track(path: Path) -> Track:
    st = path.stat()
    duration = 0.0
    bitrate = 0
    title = artist = ""
    video_id = None
    try:
        audio = mutagen.File(path)
        if audio is not None:
            duration = float(getattr(audio.info, "length", 0) or 0)
            bitrate = int(getattr(audio.info, "bitrate", 0) or 0)
            tags = audio.tags or {}
            title = _first_text(tags, "TIT2", "title", "\xa9nam")
            artist = _first_text(tags, "TPE1", "artist", "\xa9ART")
            video_id = _video_id_from_tags(tags)
    except Exception:  # noqa: BLE001,S110 - arquivo corrompido não deve derrubar a varredura
        pass
    return Track(
        path=str(path), name=path.stem, size=st.st_size, mtime=st.st_mtime,
        duration=round(duration, 1), bitrate=bitrate, title=title, artist=artist,
        video_id=video_id, key=title_key(path.stem),
    )


class Library:
    """Cache de faixas por arquivo (path, mtime, tamanho) para varreduras rápidas."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, int, Track]] = {}
        self._lock = threading.Lock()

    def scan(self, folder: str | Path) -> list[Track]:
        root = Path(folder)
        if not root.is_dir():
            return []
        tracks = []
        for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
            if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
                continue
            st = p.stat()
            with self._lock:
                hit = self._cache.get(str(p))
            if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
                tracks.append(hit[2])
                continue
            t = read_track(p)
            with self._lock:
                self._cache[str(p)] = (st.st_mtime, st.st_size, t)
            tracks.append(t)
        return tracks

    def find_existing(self, folder: str | Path, video_id: str | None, title: str) -> Track | None:
        key = title_key(title) if title else ""
        for t in self.scan(folder):
            if video_id and t.video_id == video_id:
                return t
        for t in self.scan(folder):
            if key and t.key == key:
                return t
        return None


def _file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_duplicates(tracks: list[Track]) -> list[dict]:
    """Agrupa faixas repetidas por: mesmo vídeo, mesmo arquivo ou mesmo título."""
    parent = list(range(len(tracks)))
    reasons: dict[tuple[int, int], set[str]] = {}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int, reason: str) -> None:
        a, b = find(i), find(j)
        if a != b:
            parent[b] = a
        reasons.setdefault((min(i, j), max(i, j)), set()).add(reason)

    def group_by(keyfn, reason: str) -> None:
        buckets: dict[str, list[int]] = {}
        for i, t in enumerate(tracks):
            k = keyfn(t)
            if k:
                buckets.setdefault(k, []).append(i)
        for idx in buckets.values():
            for j in idx[1:]:
                union(idx[0], j, reason)

    group_by(lambda t: t.video_id, "mesmo vídeo do YouTube")
    group_by(lambda t: t.key if len(t.key) >= 3 else None, "mesmo título")

    # Hash só para arquivos com tamanho igual (barato e exato).
    by_size: dict[int, list[int]] = {}
    for i, t in enumerate(tracks):
        by_size.setdefault(t.size, []).append(i)
    for idx in by_size.values():
        if len(idx) > 1:
            hashes: dict[str, list[int]] = {}
            for i in idx:
                try:
                    hashes.setdefault(_file_hash(tracks[i].path), []).append(i)
                except OSError:
                    continue
            for same in hashes.values():
                for j in same[1:]:
                    union(same[0], j, "arquivo idêntico")

    groups: dict[int, list[int]] = {}
    for i in range(len(tracks)):
        groups.setdefault(find(i), []).append(i)

    result = []
    for members in groups.values():
        if len(members) < 2:
            continue
        why: set[str] = set()
        for (a, b), r in reasons.items():
            if a in members and b in members:
                why |= r
        items = [tracks[i] for i in members]
        # Mantém: maior bitrate, depois nome mais limpo (curto), depois o mais antigo.
        keep = sorted(items, key=lambda t: (-t.bitrate, len(t.name), t.mtime))[0]
        result.append({
            "reasons": sorted(why),
            "keep": keep.path,
            "tracks": [t.to_dict() for t in items],
        })
    return result


def rename_plan(tracks: list[Track]) -> list[dict]:
    """Sugere nomes limpos sem colidir com arquivos existentes."""
    taken = {Path(t.path).name.lower() for t in tracks}
    plan = []
    for t in tracks:
        p = Path(t.path)
        new_stem = safe_filename(clean_title(t.name))
        new_name = new_stem + p.suffix.lower()
        if new_name == p.name:
            continue
        if new_name.lower() in taken and new_name.lower() != p.name.lower():
            continue
        taken.add(new_name.lower())
        plan.append({"from": str(p), "to": str(p.with_name(new_name)), "new_name": new_name})
    return plan


def find_junk(folder: str | Path) -> list[dict]:
    root = Path(folder)
    if not root.is_dir():
        return []
    junk = []
    files = [p for p in root.iterdir() if p.is_file()]
    audio_stems = {p.stem.lower() for p in files if p.suffix.lower() in AUDIO_EXTS}
    for p in files:
        name = p.name.lower()
        # Imagens/webm só são lixo quando sobraram ao lado do MP3 de mesmo nome
        # (uma capa "folder.jpg" proposital não é tocada).
        if name.endswith(SIDECAR_SUFFIXES):
            if p.stem.lower() in audio_stems:
                junk.append({"path": str(p), "name": p.name, "size": p.stat().st_size})
            continue
        if name.endswith(JUNK_SUFFIXES) or any(rx.search(name) for rx in JUNK_PATTERNS):
            junk.append({"path": str(p), "name": p.name, "size": p.stat().st_size})
    return junk


def ensure_inside(root: str | Path, target: str | Path) -> Path:
    """Garante que ``target`` é um arquivo dentro de ``root`` (sem path traversal)."""
    root_p = Path(root).resolve()
    tgt = Path(target).resolve()
    if tgt.parent != root_p or not tgt.is_file():
        raise ValueError(f"Arquivo fora da pasta da biblioteca: {target}")
    return tgt


def move_to_trash(root: str | Path, files: list[str]) -> list[str]:
    """Move arquivos para a lixeira do app (recuperáveis), nunca apaga direto."""
    dest = trash_dir() / time.strftime("%Y-%m-%d_%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    moved = []
    for f in files:
        src = ensure_inside(root, f)
        target = dest / src.name
        n = 1
        while target.exists():
            target = dest / f"{src.stem} ({n}){src.suffix}"
            n += 1
        shutil.move(str(src), str(target))
        moved.append(str(target))
    return moved


def apply_renames(root: str | Path, renames: list[dict]) -> list[dict]:
    done = []
    root_p = Path(root).resolve()
    for r in renames:
        src = ensure_inside(root_p, r["from"])
        new_name = safe_filename(Path(r["new_name"]).stem) + src.suffix.lower()
        dst = root_p / new_name
        if dst.exists() and dst.resolve() != src:
            continue
        src.rename(dst)
        set_title_tag(dst, Path(new_name).stem)
        done.append({"from": str(src), "to": str(dst)})
    return done


def set_title_tag(path: Path, title: str) -> None:
    if path.suffix.lower() != ".mp3":
        return
    try:
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.setall("TIT2", [TIT2(encoding=3, text=title)])
        tags.save(path)
    except Exception:  # noqa: BLE001,S110 - tag é opcional
        pass
