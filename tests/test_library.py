from pathlib import Path

import pytest

from instalador_music.library import (
    Track,
    clean_title,
    ensure_inside,
    find_duplicates,
    find_junk,
    rename_plan,
    title_key,
)


@pytest.mark.parametrize(("raw", "expected"), [
    ("flora cash - You're Somebody Else (Lyric Video)", "flora cash - You're Somebody Else"),
    ("The Neighbourhood - Daddy Issues (Official Video)", "The Neighbourhood - Daddy Issues"),
    ("SYML - Fear of the Water [Official Music Video]", "SYML - Fear of the Water"),
    ("Sasha Sloan - Too Sad To Cry ⧸⧸ Sub Español", "Sasha Sloan - Too Sad To Cry"),
    ("Dynasty (Official Music Video) ｜ MIIA", "Dynasty - MIIA"),
    ("Blackbear - ＂Idfc＂ OFFICIAL VERSION", "Blackbear - 'Idfc'"),
    ("Skyfall ~~ slowed and reverb ~~", "Skyfall ~~ slowed and reverb ~~"),
    ("505", "505"),
    ("Artista - Música (Ao Vivo)", "Artista - Música (Ao Vivo)"),
])
def test_clean_title(raw, expected):
    assert clean_title(raw) == expected


def test_title_key_ignores_noise_and_accents():
    assert title_key("Canção (Official Video)") == title_key("cancao")


def _t(path, **kw):
    base = dict(size=100, mtime=0, duration=200, bitrate=192000, title="", artist="", video_id=None)
    base.update(kw)
    name = Path(path).stem
    return Track(path=path, name=name, key=title_key(name), **base)


def test_duplicates_by_id_and_title():
    tracks = [
        _t("/m/A - B.mp3", video_id="x" * 11, size=1),
        _t("/m/A - B (Official Video).mp3", size=2, bitrate=320000),
        _t("/m/outra.mp3", video_id="x" * 11, size=3),
        _t("/m/unica.mp3", size=4),
    ]
    groups = find_duplicates(tracks)
    assert len(groups) == 1
    g = groups[0]
    assert len(g["tracks"]) == 3
    assert g["keep"] == "/m/A - B (Official Video).mp3"  # maior bitrate


def test_rename_plan_avoids_collision():
    tracks = [_t("/m/X (Official Video).mp3"), _t("/m/X.mp3"), _t("/m/Y (Lyrics).mp3")]
    plan = rename_plan(tracks)
    assert [p["new_name"] for p in plan] == ["Y.mp3"]


def test_junk_and_inside(tmp_path):
    (tmp_path / "a.mp3").write_bytes(b"x")
    (tmp_path / "a.webp").write_bytes(b"x")
    (tmp_path / "folder.jpg").write_bytes(b"x")
    (tmp_path / "b.mp3.part").write_bytes(b"x")
    names = sorted(j["name"] for j in find_junk(tmp_path))
    assert names == ["a.webp", "b.mp3.part"]
    assert ensure_inside(tmp_path, tmp_path / "a.mp3")
    with pytest.raises(ValueError):
        ensure_inside(tmp_path, tmp_path / ".." / "fora.mp3")


def test_video_id_ignores_links_in_description():
    from instalador_music.library import _video_id_from_tags

    tags = {
        "TXXX:description": "veja também https://youtu.be/AAAAAAAAAAA",
        "TXXX:purl": "https://www.youtube.com/watch?v=jbtSSo-mrOQ",
    }
    assert _video_id_from_tags(tags) == "jbtSSo-mrOQ"


def test_clean_title_stylized_unicode():
    assert clean_title("𝟑𝟎 𝐌𝐢𝐧𝐮𝐭𝐞𝐬 𝐎𝐟 𝐒𝐚𝐝 𝐌𝐮𝐬𝐢𝐜 ｜ w⧸ rain") == "30 Minutes Of Sad Music - w- rain"
