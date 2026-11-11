"""Manutenção de links: extrai, normaliza e remove duplicatas/lixo de URLs.

Regras principais:
- Vídeo do YouTube (watch, youtu.be, shorts, embed, live, music.youtube) vira
  ``https://www.youtube.com/watch?v=<id>``, sem parâmetros de rastreio.
- Links de vídeo dentro de playlist/Mix (``list=``) baixam só o vídeo.
- Mix (``list=RD...``) nunca é listado: listar Mix gigante já causou bloqueio
  (HTTP 429) do YouTube.
- Playlist pura (``/playlist?list=PL...``) só é expandida se o usuário permitir.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Literal
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

LinkKind = Literal["video", "playlist", "mix", "other", "invalid"]

YT_HOSTS = {
    "youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "youtu.be",
}
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
URL_RE = re.compile(
    r"(?:https?://|(?<![\w.])(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/)[^\s<>\"']+",
    re.IGNORECASE,
)
# Parâmetros que só servem para rastreio/navegação e podem ser descartados.
TRACKING_PARAMS = {
    "si", "feature", "pp", "ab_channel", "index", "start_radio", "t", "fbclid",
    "gclid", "igshid", "pbjreload", "app", "persist_app", "rv", "ref", "src",
}
PRIVATE_LISTS = {"LL", "WL", "LM"}  # curtidas, assistir depois, curtidas do YT Music


@dataclass
class LinkInfo:
    original: str
    url: str | None
    kind: LinkKind
    video_id: str | None = None
    playlist_id: str | None = None
    notes: list[str] = field(default_factory=list)
    duplicate: bool = False

    @property
    def key(self) -> str | None:
        if self.video_id:
            return f"yt:{self.video_id}"
        if self.playlist_id:
            return f"pl:{self.playlist_id}"
        return self.url

    def to_dict(self) -> dict:
        return asdict(self)


def _host(netloc: str) -> str:
    host = netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _strip_trailing_punct(raw: str) -> str:
    # Links colados em textos costumam vir com pontuação no fim.
    return raw.rstrip(".,;:!?)]}>'\"")


def analyze(raw: str) -> LinkInfo:
    """Analisa um único link e devolve a versão limpa + observações."""
    original = raw.strip()
    text = _strip_trailing_punct(original)
    if not text:
        return LinkInfo(original, None, "invalid", notes=["Link vazio"])
    if not re.match(r"^https?://", text, re.IGNORECASE):
        text = "https://" + text

    try:
        parts = urlsplit(text)
    except ValueError:
        return LinkInfo(original, None, "invalid", notes=["Link malformado"])

    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return LinkInfo(original, None, "invalid", notes=["Não é um link http(s)"])

    host = _host(parts.netloc)
    query = parse_qs(parts.query, keep_blank_values=False)

    if host not in YT_HOSTS:
        # Outro site suportado pelo yt-dlp: só removemos parâmetros de rastreio.
        kept = {
            k: v for k, v in query.items()
            if k not in TRACKING_PARAMS and not k.lower().startswith("utm_")
        }
        removed = sorted(set(query) - set(kept))
        clean = urlunsplit(("https", parts.netloc.lower(), parts.path, urlencode(kept, doseq=True), ""))
        notes = [f"Removido: {', '.join(removed)}"] if removed else []
        notes.append("Site fora do YouTube: o yt-dlp tentará baixar")
        return LinkInfo(original, clean, "other", notes=notes)

    path_parts = [p for p in parts.path.split("/") if p]
    video_id: str | None = None

    if host == "youtu.be" and path_parts:
        video_id = path_parts[0]
    elif path_parts and path_parts[0] in ("shorts", "embed", "live", "v", "e") and len(path_parts) > 1:
        video_id = path_parts[1]
    elif "v" in query:
        video_id = query["v"][0]

    playlist_id = query.get("list", [None])[0]
    notes: list[str] = []

    removed = sorted(k for k in query if k not in ("v", "list"))
    if removed:
        notes.append(f"Removido: {', '.join(removed)}")

    if video_id is not None and not VIDEO_ID_RE.match(video_id):
        return LinkInfo(original, None, "invalid", notes=[f"ID de vídeo inválido: {video_id!r}"])

    if video_id:
        clean = f"https://www.youtube.com/watch?v={video_id}"
        if playlist_id:
            if playlist_id.startswith("RD"):
                notes.append("Mix detectado: baixando só este vídeo")
            else:
                notes.append("Link dentro de playlist: baixando só este vídeo")
        if host == "music.youtube.com":
            notes.append("YouTube Music convertido para YouTube")
        return LinkInfo(original, clean, "video", video_id=video_id, notes=notes)

    if playlist_id:
        if playlist_id in PRIVATE_LISTS:
            return LinkInfo(
                original, None, "invalid", playlist_id=playlist_id,
                notes=["Playlist privada (curtidas/assistir depois) exige login"],
            )
        if playlist_id.startswith("RD"):
            # Mix sem vídeo: o ID do Mix normalmente termina no vídeo de origem.
            seed = playlist_id[2:]
            if VIDEO_ID_RE.match(seed):
                notes.append("Mix detectado: baixando só o vídeo de origem")
                return LinkInfo(
                    original, f"https://www.youtube.com/watch?v={seed}", "mix",
                    video_id=seed, notes=notes,
                )
            return LinkInfo(original, None, "invalid", playlist_id=playlist_id,
                            notes=["Mix sem vídeo identificável: envie o link do vídeo"])
        clean = f"https://www.youtube.com/playlist?list={playlist_id}"
        return LinkInfo(original, clean, "playlist", playlist_id=playlist_id, notes=notes)

    return LinkInfo(original, None, "invalid", notes=["Link do YouTube sem vídeo ou playlist"])


def extract_urls(text: str) -> list[str]:
    """Encontra todos os links num texto livre (uma ou várias linhas, com lixo)."""
    found = URL_RE.findall(text or "")
    # IDs soltos (11 caracteres) numa linha própria também são aceitos.
    for line in (text or "").splitlines():
        line = line.strip()
        if VIDEO_ID_RE.match(line) and not any(line in f for f in found):
            found.append(f"https://youtu.be/{line}")
    return found


def clean_batch(text: str) -> list[LinkInfo]:
    """Extrai, normaliza e marca duplicatas de um bloco de texto."""
    seen: set[str] = set()
    result: list[LinkInfo] = []
    for raw in extract_urls(text):
        info = analyze(raw)
        key = info.key
        if key and key in seen:
            info.duplicate = True
            info.notes.append("Duplicado na lista: ignorado")
        elif key:
            seen.add(key)
        result.append(info)
    return result
