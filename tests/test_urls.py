from qq_aria2_dl.bot.urls import (
    extract_urls,
    format_bytes,
    format_duration,
    normalize_url,
    progress_bar,
    sanitize_filename,
)


def test_normalize_url_lowercases_host_and_drops_fragment():
    assert normalize_url("HTTPS://Example.COM/Path?x=1#frag", ["https"]) == "https://example.com/Path?x=1"


def test_normalize_url_rejects_disallowed_scheme():
    assert normalize_url("ftp://example.com/a", ["http", "https"]) is None
    assert normalize_url("file:///etc/passwd", ["http", "https"]) is None


def test_extract_urls_trims_trailing_punctuation():
    urls = extract_urls("下载 https://example.com/a.zip，", ["http", "https"], 3)
    assert urls == ["https://example.com/a.zip"]


def test_sanitize_filename_strips_path_traversal():
    assert sanitize_filename("../../evil.exe") == "evil.exe"
    assert sanitize_filename(".") is None
    assert sanitize_filename("..") is None
    assert sanitize_filename("a/b/c.txt") == "c.txt"


def test_format_bytes():
    assert format_bytes(0) == "0 B"
    assert format_bytes(1024) == "1.00 KiB"


def test_format_duration():
    assert format_duration(30) == "30 秒"
    assert format_duration(90) == "1 分 30 秒"
    assert format_duration(3661) == "1 小时 1 分钟"


def test_progress_bar_clamped():
    assert progress_bar(50, 10) == "[█████░░░░░]"
    assert progress_bar(-5, 10) == "[░░░░░░░░░░]"
    assert progress_bar(200, 10) == "[██████████]"