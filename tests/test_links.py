from instalador_music.links import analyze, clean_batch


def test_video_strips_tracking_and_playlist():
    li = analyze("https://www.youtube.com/watch?v=qVdPh2cBTN0&list=PLabc&index=3&si=xyz&pp=1")
    assert li.kind == "video"
    assert li.url == "https://www.youtube.com/watch?v=qVdPh2cBTN0"
    assert any("playlist" in n for n in li.notes)


def test_short_forms():
    for raw in ("https://youtu.be/qVdPh2cBTN0?si=abc", "youtube.com/shorts/qVdPh2cBTN0",
                "https://music.youtube.com/watch?v=qVdPh2cBTN0&feature=share",
                "https://m.youtube.com/embed/qVdPh2cBTN0"):
        assert analyze(raw).video_id == "qVdPh2cBTN0", raw


def test_mix_never_listed():
    li = analyze("https://www.youtube.com/watch?v=qVdPh2cBTN0&list=RDqVdPh2cBTN0&start_radio=1")
    assert li.kind == "video" and "list" not in li.url
    assert any("Mix" in n for n in li.notes)
    seed = analyze("https://www.youtube.com/playlist?list=RDqVdPh2cBTN0")
    assert seed.kind == "mix" and seed.video_id == "qVdPh2cBTN0"


def test_playlist_and_private():
    assert analyze("https://www.youtube.com/playlist?list=PL1234567890").kind == "playlist"
    assert analyze("https://www.youtube.com/playlist?list=WL").kind == "invalid"


def test_invalid():
    assert analyze("https://www.youtube.com/watch?v=curto").kind == "invalid"
    assert analyze("https://www.youtube.com/@canal").kind == "invalid"


def test_batch_dedup_and_text_noise():
    text = """olha essa: https://youtu.be/qVdPh2cBTN0?si=1, e essa
    https://www.youtube.com/watch?v=qVdPh2cBTN0&t=30
    https://www.youtube.com/watch?v=jbtSSo-mrOQ."""
    out = clean_batch(text)
    assert [li.video_id for li in out] == ["qVdPh2cBTN0", "qVdPh2cBTN0", "jbtSSo-mrOQ"]
    assert [li.duplicate for li in out] == [False, True, False]


def test_other_site_keeps_url_but_strips_utm():
    li = analyze("https://soundcloud.com/a/b?utm_source=x&foo=1")
    assert li.kind == "other" and li.url == "https://soundcloud.com/a/b?foo=1"
