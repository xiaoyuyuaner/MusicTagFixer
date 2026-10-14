from __future__ import annotations

import difflib
import base64
import html
import json
import os
import re
import shutil
import sys
import threading
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from queue import Empty, Queue
from tkinter import BooleanVar, END, StringVar, Tk, filedialog, messagebox, ttk
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TDRC, TIT2, TPE1, TPE2, TRCK, USLT
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from PIL import Image

VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
if VENDOR_DIR.exists():
    sys.path.insert(0, str(VENDOR_DIR))
try:
    from opencc import OpenCC
except ImportError:
    OpenCC = None


APP_TITLE = "音乐标签与云盘修复工具"
USER_AGENT = "MusicTagFixer/0.1.0"
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*]')
UNKNOWN_VALUES = {"", "未知", "未知歌手", "unknown", "unknown artist", "kuwo", "酷我"}
MOJIBAKE_MARKERS = set("ÃÂÊÎÔÕÖÜ×½¹²µ¼¾ÀÁÈÉËÌÒÓÙÚÛÝÞßðñòóôõö÷øùúûüýþÿ")
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "MusicTagFixer"
CONFIG_FILE = CONFIG_DIR / "settings.json"
SITE_WATERMARK = re.compile(
    r"(?:\[(?:[^\]]*?(?:\.com|\.cn|\.net|\.org|www\.)[^\]]*)\]|"
    r"【(?:[^】]*?(?:公众号|微信|\.com|\.cn|\.net|\.org|www\.)[^】]*)】)",
    re.IGNORECASE,
)


@dataclass
class SongInfo:
    path: Path
    title: str
    artist: str
    album: str = ""
    year: str = ""
    track: str = ""
    duration: float = 0.0


@dataclass
class OnlineMatch:
    title: str
    artist: str
    album: str
    year: str
    track: str
    duration: float
    artwork_url: str
    score: float
    source: str = "Apple Music"
    source_id: str = ""
    lyrics_hint: str = ""


def http_json(url: str, timeout: int = 20, extra_headers: dict | None = None):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_json_post(url: str, values: dict, timeout: int = 20):
    payload = urlencode(values).encode("utf-8")
    request = Request(
        url,
        data=payload,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://music.163.com/",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_bytes(url: str, timeout: int = 30) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(ch for ch in value if ch.isalnum())


def similarity(left: str, right: str) -> float:
    a, b = normalize(left), normalize(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9
    return difflib.SequenceMatcher(None, a, b).ratio()


def safe_name(value: str) -> str:
    value = INVALID_FILENAME.sub("_", value).strip().rstrip(".")
    return value or "未命名歌曲"


def is_unknown(value: str) -> bool:
    return normalize(value) in {normalize(item) for item in UNKNOWN_VALUES}


def metadata_score(value: str) -> float:
    if not value:
        return -100
    cjk = sum("\u3400" <= ch <= "\u9fff" for ch in value)
    markers = sum(ch in MOJIBAKE_MARKERS for ch in value)
    replacements = value.count("�")
    controls = sum(ord(ch) < 32 and ch not in "\t\n\r" for ch in value)
    return cjk * 2 - markers * 3 - replacements * 8 - controls * 8


def repair_mojibake(value: str) -> str:
    value = str(value or "").strip()
    candidates = [value]
    conversions = (
        ("latin1", "gb18030"),
        ("cp1252", "gb18030"),
        ("latin1", "utf-8"),
        ("cp1252", "utf-8"),
        ("gb18030", "utf-8"),
    )
    for source, target in conversions:
        try:
            candidate = value.encode(source).decode(target).strip()
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if candidate:
            candidates.append(candidate)
    best = max(candidates, key=metadata_score)
    return best if metadata_score(best) >= metadata_score(value) + 2 else value


def clean_metadata_text(value: str) -> str:
    """Remove download-site/official-account watermarks without touching real names."""
    value = repair_mojibake(value)
    value = SITE_WATERMARK.sub("", value)
    return re.sub(r"\s{2,}", " ", value).strip(" -_－—")


def frame_text(tags: ID3 | None, key: str) -> str:
    if tags is None or key not in tags:
        return ""
    frame = tags[key]
    if hasattr(frame, "text") and frame.text:
        return str(frame.text[0]).strip()
    return str(frame).strip()


def parse_filename(path: Path) -> tuple[str, str]:
    stem = clean_metadata_text(re.sub(r"（.*?版）$", "", path.stem).strip())
    pieces = re.split(r"\s+[-–—]\s+", stem, maxsplit=1)
    if len(pieces) == 2:
        artist, title = pieces[0].strip(), pieces[1].strip()
        if is_unknown(artist):
            nested = re.split(r"\s*[-–—]\s*", title, maxsplit=1)
            if len(nested) == 2 and not is_unknown(nested[0]):
                artist, title = nested[0].strip(), nested[1].strip()
        return clean_metadata_text(title), clean_metadata_text(artist)
    return clean_metadata_text(stem), ""


def detect_format(path: Path) -> str:
    with path.open("rb") as handle:
        head = handle.read(4096)
    if head.startswith(b"fLaC"):
        return "flac"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "m4a"
    if head.startswith(b"ID3") or any(
        head[index] == 0xFF and (head[index + 1] & 0xE0) == 0xE0
        for index in range(max(0, len(head) - 1))
    ):
        return "mp3"
    raise ValueError("无法识别真实音频格式：文件可能不完整或不是受支持的音频")


def read_song(path: Path) -> SongInfo:
    title_from_name, artist_from_name = parse_filename(path)
    audio_format = detect_format(path)
    if audio_format == "mp3":
        try:
            audio = MP3(path)
        except Exception as exc:
            if "can't sync to MPEG frame" in str(exc):
                raise ValueError("文件不是有效的 MP3：可能扩展名错误、文件未下载完整或音频已损坏") from exc
            raise
        tags = audio.tags
        title = frame_text(tags, "TIT2")
        artist = frame_text(tags, "TPE1")
        album = frame_text(tags, "TALB")
        year = frame_text(tags, "TDRC")[:4]
        track = frame_text(tags, "TRCK")
    elif audio_format == "flac":
        audio = FLAC(path)
        title = (audio.get("title") or [""])[0]
        artist = (audio.get("artist") or [""])[0]
        album = (audio.get("album") or [""])[0]
        year = (audio.get("date") or [""])[0][:4]
        track = (audio.get("tracknumber") or [""])[0]
    elif audio_format == "m4a":
        audio = MP4(path)
        tags = audio.tags or {}
        title = (tags.get("\xa9nam") or [""])[0]
        artist = (tags.get("\xa9ART") or [""])[0]
        album = (tags.get("\xa9alb") or [""])[0]
        year = str((tags.get("\xa9day") or [""])[0])[:4]
        trkn = tags.get("trkn") or []
        track = str(trkn[0][0]) if trkn else ""
    else:
        raise ValueError(f"暂不支持的格式：{audio_format}")
    clean_title = title_from_name if is_unknown(str(title).strip()) else clean_metadata_text(str(title).strip())
    clean_artist = artist_from_name if is_unknown(str(artist).strip()) else clean_metadata_text(str(artist).strip())
    # A clean "artist - title" filename is the user's explicit identity hint.
    # Prefer it over stale/wrong embedded tags produced by previous repairs.
    if artist_from_name and title_from_name and not is_unknown(artist_from_name):
        clean_artist = artist_from_name
    if artist_from_name and title_from_name:
        compact_title = normalize(clean_title)
        compact_artist = normalize(artist_from_name)
        if compact_artist and compact_title.startswith(compact_artist) and normalize(title_from_name) != compact_title:
            clean_title = title_from_name
    return SongInfo(
        path=path,
        title=clean_title,
        artist=clean_artist,
        album="" if is_unknown(str(album).strip()) else clean_metadata_text(str(album).strip()),
        year=str(year).strip(),
        track=str(track).strip(),
        duration=float(audio.info.length),
    )


def search_itunes(song: SongInfo) -> OnlineMatch | None:
    query = " ".join(part for part in (song.title, song.artist) if part)
    best = None
    for country in ("CN", "TW", "HK", "US"):
        params = urlencode({"term": query, "media": "music", "entity": "song", "limit": 25, "country": country})
        try:
            data = http_json("https://itunes.apple.com/search?" + params)
        except Exception:
            continue
        for item in data.get("results", []):
            title = item.get("trackName", "")
            artist = item.get("artistName", "")
            duration = float(item.get("trackTimeMillis", 0)) / 1000
            title_score = similarity(song.title, title)
            artist_score = similarity(song.artist, artist) if song.artist else 0.7
            duration_score = 1.0 if not duration else max(0.0, 1.0 - abs(duration - song.duration) / 45.0)
            if title_score < 0.70 or (not is_unknown(song.artist) and artist_score < 0.65):
                continue
            score = title_score * 0.55 + artist_score * 0.30 + duration_score * 0.15
            match = OnlineMatch(
                title=title,
                artist=artist,
                album=item.get("collectionName", ""),
                year=str(item.get("releaseDate", ""))[:4],
                track=str(item.get("trackNumber", "") or ""),
                duration=duration,
                artwork_url=item.get("artworkUrl100", "").replace("100x100bb", "1000x1000bb"),
                score=score,
            )
            if best is None or match.score > best.score:
                best = match
        if best and best.score >= 0.90 and best.artwork_url:
            break
    return best if best and best.score >= 0.68 else None


def search_netease(song: SongInfo) -> OnlineMatch | None:
    """Use NetEase's mainland endpoint as the first metadata/cover source."""
    query = " ".join(part for part in (song.title, song.artist) if part)
    data = http_json_post(
        "https://music.163.com/api/search/get",
        {"s": query, "type": 1, "limit": 25, "offset": 0},
    )
    best = None
    for item in (data.get("result") or {}).get("songs", []):
        title = item.get("name", "")
        artist_rows = item.get("artists") or item.get("ar") or []
        artist = " / ".join(row.get("name", "") for row in artist_rows if row.get("name"))
        album_row = item.get("album") or item.get("al") or {}
        duration = float(item.get("duration") or item.get("dt") or 0) / 1000
        title_score = similarity(song.title, title)
        artist_score = similarity(song.artist, artist) if song.artist else 0.7
        duration_score = max(0.0, 1.0 - abs(duration - song.duration) / 35.0) if duration else 0.5
        if title_score < 0.70 or (not is_unknown(song.artist) and artist_score < 0.65):
            continue
        score = title_score * 0.55 + artist_score * 0.30 + duration_score * 0.15
        publish_time = album_row.get("publishTime") or item.get("publishTime") or 0
        year = ""
        if publish_time:
            try:
                from datetime import datetime
                year = str(datetime.fromtimestamp(float(publish_time) / 1000).year)
            except (ValueError, OSError, OverflowError):
                pass
        match = OnlineMatch(
            title=title,
            artist=artist,
            album=album_row.get("name", ""),
            year=year,
            track=str(item.get("no") or ""),
            duration=duration,
            artwork_url=album_row.get("picUrl", ""),
            score=score,
            source="网易云音乐",
            source_id=str(item.get("id") or ""),
        )
        if best is None or match.score > best.score:
            best = match
    if not best or best.score < 0.72:
        return None
    # The search response often omits album artwork. Enrich the winning result
    # with the domestic song-detail endpoint before falling back overseas.
    if best.source_id and not best.artwork_url:
        try:
            params = urlencode({"ids": f"[{best.source_id}]"})
            detail = http_json("https://music.163.com/api/song/detail?" + params)
            rows = detail.get("songs") or []
            if rows:
                album_row = rows[0].get("album") or rows[0].get("al") or {}
                best.artwork_url = album_row.get("picUrl", "")
                best.album = best.album or album_row.get("name", "")
        except Exception:
            pass
    return best


def search_netease_lyrics(match: OnlineMatch | None) -> str:
    if not match or match.source != "网易云音乐" or not match.source_id:
        return ""
    params = urlencode({"id": match.source_id, "lv": -1, "kv": -1, "tv": -1})
    data = http_json("https://music.163.com/api/song/lyric?" + params)
    return ((data.get("lrc") or {}).get("lyric") or "").strip()


def search_qqmusic(song: SongInfo) -> OnlineMatch | None:
    query = " ".join(part for part in (song.title, song.artist) if part)
    params = urlencode({
        "w": query, "p": 1, "n": 25, "format": "json", "new_json": 1,
        "platform": "yqq.json", "outCharset": "utf-8",
    })
    data = http_json(
        "https://c.y.qq.com/soso/fcgi-bin/client_search_cp?" + params,
        extra_headers={"Referer": "https://y.qq.com/"},
    )
    rows = (((data.get("data") or {}).get("song") or {}).get("list") or [])
    best = None
    for item in rows:
        title = item.get("songname") or item.get("name") or item.get("title") or ""
        artist_rows = item.get("singer") or []
        artist = " / ".join(row.get("name", "") for row in artist_rows if row.get("name"))
        album_row = item.get("album") or {}
        album = item.get("albumname") or album_row.get("name") or ""
        duration = float(item.get("interval") or 0)
        title_score = similarity(song.title, title)
        artist_score = similarity(song.artist, artist) if song.artist else 0.7
        duration_score = max(0.0, 1.0 - abs(duration - song.duration) / 35.0) if duration else 0.5
        if title_score < 0.70 or (not is_unknown(song.artist) and artist_score < 0.65):
            continue
        score = title_score * 0.55 + artist_score * 0.30 + duration_score * 0.15
        album_mid = item.get("albummid") or album_row.get("mid") or ""
        song_mid = item.get("songmid") or item.get("mid") or ""
        artwork = f"https://y.gtimg.cn/music/photo_new/T002R800x800M000{album_mid}.jpg" if album_mid else ""
        match = OnlineMatch(
            title=title, artist=artist, album=album,
            year=str(item.get("pubtime") or "")[:4], track=str(item.get("index_album") or ""),
            duration=duration, artwork_url=artwork, score=score,
            source="QQ音乐", source_id=str(song_mid),
        )
        if best is None or match.score > best.score:
            best = match
    return best if best and best.score >= 0.72 else None


def search_qqmusic_lyrics(match: OnlineMatch | None) -> str:
    if not match or match.source != "QQ音乐" or not match.source_id:
        return ""
    params = urlencode({
        "songmid": match.source_id, "format": "json", "nobase64": 1,
        "outCharset": "utf-8",
    })
    data = http_json(
        "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?" + params,
        extra_headers={"Referer": "https://y.qq.com/"},
    )
    return html.unescape(data.get("lyric") or "").strip()


def search_kugou(song: SongInfo) -> OnlineMatch | None:
    query = " ".join(part for part in (song.title, song.artist) if part)
    params = urlencode({"keyword": query, "page": 1, "pagesize": 25, "platform": "WebFilter"})
    data = http_json(
        "https://songsearch.kugou.com/song_search_v2?" + params,
        extra_headers={"Referer": "https://www.kugou.com/"},
    )
    rows = ((data.get("data") or {}).get("lists") or [])
    best = None
    for item in rows:
        title = item.get("SongName") or item.get("OriSongName") or ""
        artist = item.get("SingerName") or ""
        duration = float(item.get("Duration") or 0)
        title_score = similarity(song.title, title)
        artist_score = similarity(song.artist, artist) if song.artist else 0.7
        duration_score = max(0.0, 1.0 - abs(duration - song.duration) / 35.0) if duration else 0.5
        if title_score < 0.70 or (not is_unknown(song.artist) and artist_score < 0.65):
            continue
        score = title_score * 0.55 + artist_score * 0.30 + duration_score * 0.15
        file_hash = item.get("FileHash") or item.get("SQFileHash") or ""
        album_id = str(item.get("AlbumID") or "")
        cover_url = item.get("Image") or (item.get("trans_param") or {}).get("union_cover") or ""
        cover_url = cover_url.replace("{size}", "800").replace("http://", "https://", 1)
        match = OnlineMatch(
            title=title, artist=artist, album=item.get("AlbumName") or "",
            year=str(item.get("PublishTime") or "")[:4], track="",
            duration=duration, artwork_url=cover_url, score=score,
            source="酷狗音乐", source_id=f"{file_hash}|{album_id}",
        )
        if best is None or match.score > best.score:
            best = match
    if not best or best.score < 0.72:
        return None
    return best


def search_kugou_lyrics(match: OnlineMatch | None) -> str:
    if not match or match.source != "酷狗音乐" or not match.source_id:
        return ""
    if match.lyrics_hint:
        return match.lyrics_hint.strip()
    file_hash, _album_id = match.source_id.split("|", 1)
    params = urlencode({
        "ver": 1, "man": "yes", "client": "mobi",
        "keyword": f"{match.artist} - {match.title}",
        "duration": int(match.duration * 1000), "hash": file_hash,
    })
    result = http_json("https://krcs.kugou.com/search?" + params)
    candidates = result.get("candidates") or []
    if not candidates:
        return ""
    candidate = candidates[0]
    params = urlencode({
        "ver": 1, "client": "pc", "id": candidate.get("id"),
        "accesskey": candidate.get("accesskey"), "fmt": "lrc", "charset": "utf8",
    })
    payload = http_json("https://lyrics.kugou.com/download?" + params)
    content = payload.get("content") or ""
    if not content:
        return ""
    return base64.b64decode(content).decode("utf-8", errors="replace").strip()


def search_musicbrainz(song: SongInfo) -> OnlineMatch | None:
    clauses = [f'recording:"{song.title}"']
    if song.artist:
        clauses.append(f'artist:"{song.artist}"')
    params = urlencode({"query": " AND ".join(clauses), "fmt": "json", "limit": 15})
    data = http_json("https://musicbrainz.org/ws/2/recording/?" + params)
    best = None
    for row in data.get("recordings", []):
        title = row.get("title", "")
        artist = "".join(
            str(part.get("name", "")) + str(part.get("joinphrase", ""))
            for part in row.get("artist-credit", [])
        ).strip()
        duration = float(row.get("length") or 0) / 1000
        title_score = similarity(song.title, title)
        artist_score = similarity(song.artist, artist) if song.artist else 0.7
        duration_score = max(0.0, 1.0 - abs(duration - song.duration) / 45.0) if duration else 0.5
        if title_score < 0.70 or (not is_unknown(song.artist) and artist_score < 0.65):
            continue
        score = title_score * 0.55 + artist_score * 0.30 + duration_score * 0.15

        releases = row.get("releases") or []
        release = releases[0] if releases else {}
        release_group = release.get("release-group") or {}
        group_id = release_group.get("id", "")
        artwork_url = (
            f"https://coverartarchive.org/release-group/{group_id}/front-500"
            if group_id
            else ""
        )
        match = OnlineMatch(
            title=title,
            artist=artist,
            album=release.get("title", ""),
            year=str(release.get("date", ""))[:4],
            track="",
            duration=duration,
            artwork_url=artwork_url,
            score=score,
            source="MusicBrainz",
        )
        if best is None or match.score > best.score:
            best = match
    return best if best and best.score >= 0.68 else None


def find_local_cover(path: Path) -> Path | None:
    extensions = (".jpg", ".jpeg", ".png", ".webp")
    names = [path.stem, "cover", "folder", "front", "album"]
    files = {item.name.casefold(): item for item in path.parent.iterdir() if item.is_file()}
    for name in names:
        for extension in extensions:
            candidate = files.get((name + extension).casefold())
            if candidate:
                return candidate
    return None


def search_lrclib(song: SongInfo, online: OnlineMatch | None) -> str:
    artists = [song.artist]
    titles = [song.title]
    if online:
        artists.insert(0, online.artist)
        titles.insert(0, online.title)

    candidates = []
    seen = set()
    for title, artist in zip(titles, artists):
        params = urlencode({"track_name": title, "artist_name": artist})
        try:
            rows = http_json("https://lrclib.net/api/search?" + params)
        except Exception:
            continue
        for row in rows:
            key = row.get("id")
            if key not in seen:
                seen.add(key)
                candidates.append(row)

    best_text = ""
    best_score = 0.0
    for row in candidates:
        synced = (row.get("syncedLyrics") or "").strip()
        if not synced or not re.search(r"\[\d{1,3}:\d{2}(?:\.\d+)?]", synced):
            continue
        title_score = similarity(song.title, row.get("trackName", ""))
        artist_score = similarity(song.artist, row.get("artistName", "")) if song.artist else 0.7
        duration = float(row.get("duration") or 0)
        duration_score = max(0.0, 1.0 - abs(duration - song.duration) / 25.0) if duration else 0.5
        if title_score < 0.70 or (not is_unknown(song.artist) and artist_score < 0.65):
            continue
        score = title_score * 0.50 + artist_score * 0.25 + duration_score * 0.25
        if score > best_score:
            best_score, best_text = score, synced
    return best_text if best_score >= 0.65 else ""


def existing_lyrics(tags: ID3 | None) -> str:
    if tags is None:
        return ""
    frames = tags.getall("USLT")
    timed = [frame.text for frame in frames if re.search(r"\[\d{1,3}:\d{2}(?:\.\d+)?]", frame.text)]
    if timed:
        return max(timed, key=len)
    return max((frame.text for frame in frames), key=len, default="")


def existing_cover(tags: ID3 | None) -> bytes:
    if tags is None:
        return b""
    frames = tags.getall("APIC")
    return max((frame.data for frame in frames), key=len, default=b"")


def read_existing_media(path: Path) -> tuple[str, bytes]:
    audio_format = detect_format(path)
    if audio_format == "mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = None
        return existing_lyrics(tags), existing_cover(tags)
    if audio_format == "flac":
        audio = FLAC(path)
        lyrics = ""
        for key in ("syncedlyrics", "lyrics", "unsyncedlyrics"):
            values = audio.get(key) or []
            if values:
                timed = [value for value in values if re.search(r"\[\d{1,3}:\d{2}", value)]
                lyrics = max(timed or values, key=len)
                if timed:
                    break
        cover = max((picture.data for picture in audio.pictures), key=len, default=b"")
        return lyrics, cover
    if audio_format == "m4a":
        audio = MP4(path)
        tags = audio.tags or {}
        lyrics = max((str(value) for value in tags.get("\xa9lyr", [])), key=len, default="")
        cover = max((bytes(value) for value in tags.get("covr", [])), key=len, default=b"")
        return lyrics, cover
    return "", b""


def normalize_cover(data: bytes) -> bytes:
    image = Image.open(BytesIO(data)).convert("RGB")
    if image.width != image.height:
        side = max(image.width, image.height)
        canvas = Image.new("RGB", (side, side), (18, 18, 18))
        image.thumbnail((side, side), Image.Resampling.LANCZOS)
        canvas.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
        image = canvas
    image.thumbnail((1000, 1000), Image.Resampling.LANCZOS)
    output = BytesIO()
    image.save(output, "JPEG", quality=90, optimize=True, progressive=False)
    return output.getvalue()


def simplify_chinese(text: str) -> str:
    if not text or OpenCC is None:
        return text
    simplified = OpenCC("t2s").convert(text)
    # OpenCC intentionally keeps some gendered traditional pronouns. Mainland
    # simplified lyrics conventionally writes all of these as “你”.
    return simplified.translate(str.maketrans({"妳": "你", "祢": "你"}))


def simplify_lyrics(text: str) -> str:
    return simplify_chinese(text)


def write_tags(
    output: Path,
    title: str,
    artist: str,
    album: str,
    year: str,
    track: str,
    cover: bytes,
    lyrics: str,
):
    suffix = output.suffix.lower()
    if suffix == ".mp3":
        ID3().delete(output)
        tags = ID3()
        tags.add(TIT2(encoding=1, text=title))
        if artist:
            tags.add(TPE1(encoding=1, text=artist))
            tags.add(TPE2(encoding=1, text=artist))
        if album:
            tags.add(TALB(encoding=1, text=album))
        if year:
            tags.add(TDRC(encoding=0, text=year[:4]))
        if track:
            tags.add(TRCK(encoding=1, text=track))
        if cover:
            # Empty APIC description is required by the known-good NetEase sample.
            tags.add(APIC(encoding=0, mime="image/jpeg", type=3, desc="", data=cover))
        if lyrics:
            tags.add(USLT(encoding=1, lang="eng", desc="", text=lyrics))
        tags.save(output, v2_version=3)
        return

    if suffix == ".flac":
        audio = FLAC(output)
        audio.delete()
        audio["title"] = title
        if artist:
            audio["artist"] = artist
        if album:
            audio["album"] = album
        if year:
            audio["date"] = year[:4]
        if track:
            audio["tracknumber"] = track
        if lyrics:
            audio["syncedlyrics"] = lyrics
            audio["lyrics"] = lyrics
        audio.clear_pictures()
        if cover:
            picture = Picture()
            picture.type = 3
            picture.mime = "image/jpeg"
            picture.desc = ""
            image = Image.open(BytesIO(cover))
            picture.width, picture.height = image.size
            picture.depth = 24
            picture.data = cover
            audio.add_picture(picture)
        audio.save()
        return

    if suffix in {".m4a", ".mp4"}:
        audio = MP4(output)
        audio.delete()
        if audio.tags is None:
            audio.add_tags()
        audio["\xa9nam"] = [title]
        if artist:
            audio["\xa9ART"] = [artist]
        if album:
            audio["\xa9alb"] = [album]
        if year:
            audio["\xa9day"] = [year[:4]]
        if track:
            number = int(re.match(r"\d+", track).group()) if re.match(r"\d+", track) else 0
            if number:
                audio["trkn"] = [(number, 0)]
        if lyrics:
            audio["\xa9lyr"] = [lyrics]
        if cover:
            audio["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()
        return
    raise ValueError(f"暂不支持的格式：{suffix}")


def process_file(
    path: Path,
    output_dir: Path,
    use_online: bool,
    log,
    manual_cover: Path | None = None,
) -> Path:
    song = read_song(path)
    old_lyrics, old_cover = read_existing_media(path)

    online = None
    musicbrainz = None
    if use_online:
        try:
            online = search_netease(song)
        except Exception as exc:
            log(f"  网易云音乐查询失败：{exc}")
        if online is None:
            try:
                online = search_qqmusic(song)
            except Exception as exc:
                log(f"  QQ音乐查询失败：{exc}")
        if online is None:
            try:
                online = search_kugou(song)
            except Exception as exc:
                log(f"  酷狗音乐查询失败：{exc}")
        if online is None:
            try:
                online = search_itunes(song)
            except Exception as exc:
                log(f"  Apple Music 查询失败：{exc}")
        if online is None:
            try:
                musicbrainz = search_musicbrainz(song)
                online = musicbrainz
            except Exception as exc:
                log(f"  MusicBrainz 查询失败：{exc}")
    if online:
        log(f"  {online.source} 匹配：{online.artist} - {online.title}（可信度 {online.score:.0%}）")
    else:
        log("  未找到足够可靠的在线信息，保留原标签")

    lyrics = ""
    if use_online:
        try:
            lyrics = search_netease_lyrics(online)
            if lyrics:
                log("  歌词源：网易云音乐")
        except Exception as exc:
            log(f"  网易云音乐歌词查询失败：{exc}")
        if not lyrics:
            try:
                qq_lyric_match = online if online and online.source == "QQ音乐" else search_qqmusic(song)
                lyrics = search_qqmusic_lyrics(qq_lyric_match)
                if lyrics:
                    log("  歌词源：QQ音乐")
            except Exception as exc:
                log(f"  QQ音乐歌词查询失败：{exc}")
        if not lyrics:
            try:
                kugou_lyric_match = online if online and online.source == "酷狗音乐" else search_kugou(song)
                lyrics = search_kugou_lyrics(kugou_lyric_match)
                if lyrics:
                    log("  歌词源：酷狗音乐")
            except Exception as exc:
                log(f"  酷狗音乐歌词查询失败：{exc}")
        if not lyrics:
            try:
                lyrics = search_lrclib(song, online)
                if lyrics:
                    log("  歌词源：LRCLIB")
            except Exception as exc:
                log(f"  LRCLIB 时间轴歌词查询失败：{exc}")
    if not lyrics:
        lyrics = old_lyrics
    lyrics = simplify_lyrics(lyrics)
    log(f"  歌词：{'时间轴歌词' if re.search(r'\[\d{1,3}:\d{2}', lyrics) else ('普通歌词' if lyrics else '无')} ")

    cover = b""
    if manual_cover:
        try:
            cover = normalize_cover(manual_cover.read_bytes())
            log(f"  封面：手动指定 {manual_cover.name}")
        except Exception as exc:
            log(f"  手动封面读取失败：{exc}")
    if not cover:
        if old_cover:
            cover = normalize_cover(old_cover)
            log("  封面：保留并规范原封面")
    if not cover:
        local_cover = find_local_cover(path)
        if local_cover:
            try:
                cover = normalize_cover(local_cover.read_bytes())
                log(f"  封面：同目录图片 {local_cover.name}")
            except Exception as exc:
                log(f"  同目录封面读取失败：{exc}")
    if not cover and online and online.artwork_url:
        try:
            cover = normalize_cover(http_bytes(online.artwork_url))
            log(f"  封面：{online.source} 正式封面")
        except Exception as exc:
            log(f"  {online.source} 封面获取失败：{exc}")
    if not cover and use_online:
        for source_name, lookup in (("QQ音乐", search_qqmusic), ("酷狗音乐", search_kugou)):
            if online and online.source == source_name:
                continue
            try:
                alternate = lookup(song)
                if alternate and alternate.artwork_url:
                    cover = normalize_cover(http_bytes(alternate.artwork_url))
                    log(f"  封面：{source_name} 正式封面")
                    break
            except Exception as exc:
                log(f"  {source_name}封面查询失败：{exc}")
    if not cover and use_online and (online is None or online.source != "MusicBrainz"):
        try:
            musicbrainz = musicbrainz or search_musicbrainz(song)
            if musicbrainz and musicbrainz.artwork_url:
                cover = normalize_cover(http_bytes(musicbrainz.artwork_url))
                log("  封面：Cover Art Archive 正式封面")
        except Exception as exc:
            log(f"  Cover Art Archive 查询失败：{exc}")
    if not cover:
        log("  封面：未找到，请使用“指定封面”")

    # Local title/artist are identity fields. Online matches may enrich missing
    # values, but must never rename a song that already has usable local tags.
    final_title = simplify_chinese(song.title or (online.title if online else ""))
    final_artist = simplify_chinese(song.artist if not is_unknown(song.artist) else (online.artist if online else ""))
    if online and online.source == "MusicBrainz":
        # MusicBrainz recordings can appear on many compilations. Prefer an
        # existing album instead of replacing it with the first compilation.
        final_album = simplify_chinese(song.album or online.album or song.title)
        final_year = song.year or online.year
        final_track = song.track or online.track
    else:
        final_album = simplify_chinese(online.album if online else (song.album or song.title))
        final_year = online.year if online else song.year
        final_track = online.track if online else song.track

    issues = []
    if is_unknown(final_artist):
        issues.append("歌手缺失")
    if not lyrics:
        issues.append("无歌词")
    if not cover:
        issues.append("无封面")
    category = "待复查" if issues else "处理成功"
    output_dir = output_dir / category
    output_dir.mkdir(parents=True, exist_ok=True)
    extension = "." + detect_format(path)
    output = output_dir / f"{safe_name(final_artist)} - {safe_name(final_title)}{extension}"
    suffix = 2
    while output.exists():
        output = output_dir / f"{safe_name(final_artist)} - {safe_name(final_title)} ({suffix}){extension}"
        suffix += 1
    shutil.copy2(path, output)
    write_tags(output, final_title, final_artist, final_album, final_year, final_track, cover, lyrics)
    if issues:
        log(f"  结果：待复查（{'、'.join(issues)}）")
    else:
        log("  结果：处理成功")
    return output


class App:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("850x620")
        self.files: list[Path] = []
        self.manual_covers: dict[Path, Path] = {}
        self.queue: Queue = Queue()
        self.run_event = threading.Event()
        self.run_event.set()
        self.is_paused = False
        settings = self.load_settings()
        default_output = Path.home() / "Desktop" / "音乐标签修复结果"
        self.output_var = StringVar(value=settings.get("output_dir", str(default_output)))
        self.online_var = BooleanVar(value=True)
        self.status_var = StringVar(value="请选择音频文件")
        self.build_ui()
        self.root.after(100, self.poll_queue)

    @staticmethod
    def load_settings() -> dict:
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}

    def save_settings(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(
                json.dumps({"output_dir": self.output_var.get().strip()}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def build_ui(self):
        frame = ttk.Frame(self.root, padding=14)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text=APP_TITLE, font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="批量补全正式封面与时间轴歌词；支持 MP3、FLAC、M4A，不覆盖原文件。",
        ).pack(anchor="w", pady=(4, 12))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="添加歌曲", command=self.add_files).pack(side="left")
        ttk.Button(buttons, text="添加文件夹", command=self.add_folder).pack(side="left", padx=8)
        ttk.Button(buttons, text="指定封面", command=self.choose_manual_cover).pack(side="left")
        ttk.Button(buttons, text="清空", command=self.clear_files).pack(side="left")

        self.listbox = __import__("tkinter").Listbox(frame, height=13, selectmode="extended")
        self.listbox.pack(fill="both", expand=True, pady=10)

        output_row = ttk.Frame(frame)
        output_row.pack(fill="x")
        ttk.Label(output_row, text="输出目录：").pack(side="left")
        ttk.Entry(output_row, textvariable=self.output_var).pack(side="left", fill="x", expand=True)
        ttk.Button(output_row, text="选择", command=self.choose_output).pack(side="left", padx=(8, 0))

        options = ttk.Frame(frame)
        options.pack(fill="x", pady=10)
        ttk.Checkbutton(
            options,
            text="联网查找正式发行信息、封面和时间轴歌词（找不到时保留原内容）",
            variable=self.online_var,
        ).pack(side="left")

        action = ttk.Frame(frame)
        action.pack(fill="x")
        self.start_button = ttk.Button(action, text="开始修复", command=self.start)
        self.start_button.pack(side="left")
        self.pause_button = ttk.Button(action, text="暂停", command=self.toggle_pause, state="disabled")
        self.pause_button.pack(side="left", padx=(8, 0))
        self.progress = ttk.Progressbar(action, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=12)
        ttk.Label(action, textvariable=self.status_var).pack(side="right")

        self.logbox = __import__("tkinter").Text(frame, height=10, state="disabled", wrap="word")
        self.logbox.pack(fill="both", expand=True, pady=(10, 0))

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="选择歌曲",
            filetypes=[("支持的音频", "*.mp3 *.flac *.m4a"), ("MP3", "*.mp3"), ("FLAC", "*.flac"), ("M4A", "*.m4a")],
        )
        self.add_paths(Path(path) for path in paths)

    def add_folder(self):
        folder = filedialog.askdirectory(title="选择包含音频文件的文件夹")
        if folder:
            root = Path(folder)
            self.add_paths(sorted(path for path in root.rglob("*") if path.suffix.lower() in {".mp3", ".flac", ".m4a"}))

    def add_paths(self, paths):
        known = {path.resolve() for path in self.files}
        for path in paths:
            resolved = path.resolve()
            if resolved not in known:
                self.files.append(path)
                known.add(resolved)
                self.listbox.insert(END, str(path))
        self.status_var.set(f"已选择 {len(self.files)} 首")

    def clear_files(self):
        self.files.clear()
        self.manual_covers.clear()
        self.listbox.delete(0, END)
        self.status_var.set("请选择音频文件")

    def choose_output(self):
        current = self.output_var.get().strip()
        folder = filedialog.askdirectory(
            title="选择输出目录",
            initialdir=current if current and Path(current).exists() else str(Path.home()),
        )
        if folder:
            self.output_var.set(folder)
            self.save_settings()

    def choose_manual_cover(self):
        selected = self.listbox.curselection()
        if len(selected) != 1:
            messagebox.showwarning(APP_TITLE, "请先在歌曲列表中只选择一首歌曲。")
            return
        image_path = filedialog.askopenfilename(
            title="选择封面图片",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.webp")],
        )
        if not image_path:
            return
        index = selected[0]
        song_path = self.files[index]
        self.manual_covers[song_path.resolve()] = Path(image_path)
        self.listbox.delete(index)
        self.listbox.insert(index, f"{song_path}    [封面：{Path(image_path).name}]")
        self.listbox.selection_set(index)

    def write_log(self, text: str):
        self.logbox.configure(state="normal")
        self.logbox.insert(END, text + "\n")
        self.logbox.see(END)
        self.logbox.configure(state="disabled")

    def start(self):
        if not self.files:
            messagebox.showwarning(APP_TITLE, "请先添加至少一个 MP3、FLAC 或 M4A 文件。")
            return
        output = Path(self.output_var.get().strip())
        if not str(output):
            messagebox.showwarning(APP_TITLE, "请选择输出目录。")
            return
        self.save_settings()
        self.start_button.configure(state="disabled")
        self.pause_button.configure(state="normal", text="暂停")
        self.is_paused = False
        self.run_event.set()
        self.progress.configure(maximum=len(self.files), value=0)
        self.write_log("开始处理。原文件不会被修改。")
        threading.Thread(target=self.worker, args=(list(self.files), output), daemon=True).start()

    def toggle_pause(self):
        if self.is_paused:
            self.is_paused = False
            self.run_event.set()
            self.pause_button.configure(text="暂停")
            self.status_var.set("继续处理中")
            self.write_log("已继续处理。")
        else:
            self.is_paused = True
            self.run_event.clear()
            self.pause_button.configure(text="继续")
            self.status_var.set("将在当前歌曲完成后暂停")
            self.write_log("已请求暂停；当前歌曲完成后暂停。")

    def worker(self, files: list[Path], output: Path):
        succeeded = 0
        complete = 0
        review = 0
        for index, path in enumerate(files, 1):
            self.run_event.wait()
            self.queue.put(("log", f"\n[{index}/{len(files)}] {path.name}"))
            try:
                result = process_file(
                    path,
                    output,
                    self.online_var.get(),
                    lambda msg: self.queue.put(("log", msg)),
                    self.manual_covers.get(path.resolve()),
                )
                self.queue.put(("log", f"  完成：{result.name}"))
                succeeded += 1
                if result.parent.name == "处理成功":
                    complete += 1
                else:
                    review += 1
            except Exception as exc:
                self.queue.put(("log", f"  失败：{exc}"))
            self.queue.put(("progress", index))
        self.queue.put(("done", (succeeded, len(files), complete, review, output)))

    def poll_queue(self):
        try:
            while True:
                kind, value = self.queue.get_nowait()
                if kind == "log":
                    self.write_log(value)
                elif kind == "progress":
                    self.progress.configure(value=value)
                    self.status_var.set(f"处理中 {value}/{len(self.files)}")
                elif kind == "done":
                    succeeded, total, complete, review, output = value
                    self.start_button.configure(state="normal")
                    self.pause_button.configure(state="disabled", text="暂停")
                    self.is_paused = False
                    self.run_event.set()
                    self.status_var.set(f"完成 {succeeded}/{total}")
                    messagebox.showinfo(
                        APP_TITLE,
                        f"处理完成：{succeeded}/{total}\n"
                        f"处理成功：{complete}\n待复查：{review}\n"
                        f"输出目录：{output}",
                    )
        except Empty:
            pass
        self.root.after(100, self.poll_queue)


def main():
    root = Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
