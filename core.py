"""
Core (GUI-independent) logic for YTScrape:
  * environment discovery (ffmpeg, JS runtime)
  * link inspection for previews
  * yt-dlp option building + downloading
  * chaining several media files into one with chapters + timestamp list
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yt_dlp

APP_NAME = "YTScrape"
APP_DIR = Path(__file__).resolve().parent
LOCAL_BIN = APP_DIR / "bin"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

VIDEO_QUALITIES = ["Best", "2160p", "1440p", "1080p", "720p", "480p", "360p"]
VIDEO_CONTAINERS = ["mp4", "mkv", "webm"]
AUDIO_FORMATS = ["mp3", "m4a", "opus", "flac", "wav"]
AUDIO_BITRATES = ["320", "256", "192", "128", "96"]


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #
def find_ffmpeg() -> str | None:
    """Directory containing ffmpeg: PATH -> ./bin -> bundled imageio-ffmpeg."""
    exe = shutil.which("ffmpeg")
    if exe:
        return str(Path(exe).parent)
    local = LOCAL_BIN / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if local.exists():
        return str(LOCAL_BIN)
    try:
        import imageio_ffmpeg

        LOCAL_BIN.mkdir(exist_ok=True)
        shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), local)
        return str(LOCAL_BIN)
    except Exception:
        return None


def ffmpeg_exe(ffmpeg_dir: str) -> str:
    return str(Path(ffmpeg_dir) / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"))


def find_js_runtime() -> dict:
    return {n: {} for n in ("deno", "node", "bun") if shutil.which(n)}


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def human_bytes(n) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_duration(sec) -> str:
    if not sec:
        return "–"
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def human_count(n) -> str:
    if n is None:
        return ""
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + suf
    return str(n)


def stamp(sec: float, force_hours: bool) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if force_hours else f"{m:02d}:{s:02d}"


_JUNK = (
    r"official|music\s*video|video|audio|lyrics?|lyric\s*video|visuali[sz]er|hd|hq|4k|8k|1080p|"
    r"720p|mv|m/v|remaster(?:ed)?(?:\s*\d{4})?|\d{4}\s*remaster(?:ed)?|explicit|clean|full\s*album|"
    r"free\s*download|out\s*now|premiere|live|color\s*coded|eng\s*sub"
)
_BRACKET_RE = re.compile(r"\s*[\(\[\{【]([^\)\]\}】]*)[\)\]\}】]")


def clean_title(title: str) -> str:
    """Strip noise like '(Official Video)', '[4K]', '| Lyrics' from a title."""
    t = title or ""

    def drop(m):
        inner = m.group(1).strip().lower()
        return "" if re.fullmatch(rf"(?:{_JUNK})(?:[\s\-/&,+]+(?:{_JUNK}))*", inner) else m.group(0)

    t = _BRACKET_RE.sub(drop, t)
    t = re.sub(rf"\s*[|｜]\s*(?:{_JUNK}).*$", "", t, flags=re.I)
    t = re.sub(r"\s*#\w+", "", t)  # hashtags
    t = re.sub(r"\s{2,}", " ", t).strip(" -–—|")
    return t or (title or "").strip()


def build_timestamps(items: list[tuple[str, float]]) -> list[tuple[float, str]]:
    """[(title, duration)] -> [(start_seconds, title)]"""
    out, t = [], 0.0
    for title, dur in items:
        out.append((t, title))
        t += dur or 0
    return out


def format_timestamps(stamps: list[tuple[float, str]], total: float | None = None) -> str:
    hours = (total or (stamps[-1][0] if stamps else 0)) >= 3600
    return "\n".join(f"{stamp(s, hours)} {t}" for s, t in stamps)


# --------------------------------------------------------------------------- #
# Link inspection (for previews)
# --------------------------------------------------------------------------- #
@dataclass
class LinkItem:
    url: str
    id: str = ""
    title: str = ""
    channel: str = ""
    duration: float | None = None
    views: int | None = None
    upload_date: str = ""
    thumbnail: str = ""
    playlist: str = ""
    # None = not known yet (e.g. flat playlist entry); [] = known to have none
    chapters: list[dict] | None = None
    description: str = ""


def _thumb_for(e: dict) -> str:
    vid = e.get("id")
    ie = (e.get("ie_key") or e.get("extractor_key") or "").lower()
    if vid and ("youtube" in ie or "youtu" in (e.get("url") or e.get("webpage_url") or "")):
        return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
    if e.get("thumbnail"):
        return e["thumbnail"]
    thumbs = e.get("thumbnails") or []
    return thumbs[-1]["url"] if thumbs else ""


def _item_from(e: dict, playlist: str = "") -> LinkItem:
    url = e.get("webpage_url") or e.get("url") or ""
    if url and not url.startswith("http") and e.get("id"):
        url = f"https://www.youtube.com/watch?v={e['id']}"
    full = "formats" in e  # a fully extracted video (not a flat playlist entry)
    return LinkItem(
        chapters=chapters_from_info(e) if full else None,
        description=e.get("description") or "",
        url=url,
        id=e.get("id") or "",
        title=e.get("title") or url,
        channel=e.get("channel") or e.get("uploader") or "",
        duration=e.get("duration"),
        views=e.get("view_count"),
        upload_date=e.get("upload_date") or "",
        thumbnail=_thumb_for(e),
        playlist=playlist,
    )


def fetch_details(url: str, js: dict, cookies_browser: str | None = None) -> LinkItem:
    """Full extraction of one video (gets chapters + description)."""
    return inspect_link(url, js, expand_playlists=False, cookies_browser=cookies_browser)[0]


def inspect_link(url: str, js: dict, expand_playlists: bool = True, limit: int = 300,
                 cookies_browser: str | None = None) -> list[LinkItem]:
    """Resolve a URL into one or more previewable items (playlists are expanded)."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": "in_playlist", "noplaylist": not expand_playlists,
            "playlistend": limit}
    if js:
        opts["js_runtimes"] = js
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser.lower(),)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info.get("_type") in ("playlist", "multi_video") or info.get("entries") is not None:
        title = info.get("title") or ""
        items = []

        def walk(entries):
            for e in entries or []:
                if not e:
                    continue
                if e.get("_type") == "playlist" or (e.get("entries") is not None):
                    walk(e.get("entries"))  # channel tabs nest playlists
                elif len(items) < limit:
                    items.append(_item_from(e, title))

        walk(info.get("entries"))
        return items
    return [_item_from(info)]


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def build_ydl_opts(o: dict, ffmpeg_dir: str | None, js: dict) -> dict:
    out_dir = o["out_dir"]
    sub = o.get("subfolder") or ""
    tmpl = os.path.join(sub, "%(title)s [%(id)s].%(ext)s") if sub else "%(title)s [%(id)s].%(ext)s"

    opts: dict = {
        "paths": {"home": out_dir, "temp": os.path.join(out_dir, ".ytscrape_tmp")},
        "outtmpl": {"default": tmpl},
        "windowsfilenames": True,
        "trim_file_name": 180,
        "noplaylist": True,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "file_access_retries": 5,
        "continuedl": True,
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 30,
        "overwrites": False,
        "quiet": True,
        "noprogress": True,
        "postprocessors": [],
    }
    if ffmpeg_dir:
        opts["ffmpeg_location"] = ffmpeg_dir
    if js:
        opts["js_runtimes"] = js
    if o.get("rate_limit"):
        try:
            opts["ratelimit"] = float(o["rate_limit"]) * 1024 * 1024
        except ValueError:
            pass
    if o.get("archive"):
        opts["download_archive"] = os.path.join(out_dir, "downloaded_archive.txt")
    if o.get("cookies_browser") and o["cookies_browser"] != "None":
        opts["cookiesfrombrowser"] = (o["cookies_browser"].lower(),)

    pps = opts["postprocessors"]
    if o["mode"] == "audio":
        afmt = o["audio_format"]
        opts["format"] = "bestaudio/best"
        pp = {"key": "FFmpegExtractAudio", "preferredcodec": afmt}
        if afmt in ("mp3", "m4a", "opus"):
            pp["preferredquality"] = o["audio_bitrate"]
        pps.append(pp)
    else:
        q = o["video_quality"]
        h = None if q == "Best" else int(q.rstrip("p"))
        hf = f"[height<={h}]" if h else ""
        container = o["container"]
        if o["compat"] and container == "mp4":
            opts["format"] = (f"bv*{hf}[vcodec^=avc1]+ba[acodec^=mp4a]/"
                              f"bv*{hf}[ext=mp4]+ba[ext=m4a]/bv*{hf}+ba/b{hf}/b")
            opts["format_sort"] = ["vcodec:h264", "res", "acodec:aac"]
            pps.append({"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"})
        else:
            opts["format"] = f"bv*{hf}+ba/b{hf}/b"
            if h:
                opts["format_sort"] = [f"res:{h}"]
        opts["merge_output_format"] = container

    if o.get("subs"):
        opts.update(writesubtitles=True, writeautomaticsub=True,
                    subtitleslangs=[s.strip() for s in o.get("sub_langs", "en").split(",") if s.strip()] or ["en"],
                    subtitlesformat="srt/best")
        pps.append({"key": "FFmpegSubtitlesConvertor", "format": "srt"})
        if o["mode"] == "video" and o["container"] in ("mp4", "mkv"):
            pps.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": True})
    if o.get("metadata"):
        pps.append({"key": "FFmpegMetadata", "add_chapters": True, "add_metadata": True})
    if o.get("thumbnail"):
        opts["writethumbnail"] = True
        can_embed = (o["mode"] == "audio" and o["audio_format"] in ("mp3", "m4a", "flac", "opus")) or (
            o["mode"] == "video" and o["container"] in ("mp4", "mkv"))
        pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
        if can_embed:
            pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    if o.get("info_json"):
        opts["writeinfojson"] = True
        opts["writedescription"] = True
    return opts


def download_one(url: str, opts: dict, cancel: threading.Event,
                 on_progress: Callable[[dict], None], logger=None) -> tuple[str | None, dict]:
    """Download a single URL. Returns (final file path or None, info dict)."""
    final: dict = {}

    def phook(d):
        if cancel.is_set():
            raise Cancelled()
        on_progress(d)

    def pphook(d):
        if cancel.is_set():
            raise Cancelled()
        if d["status"] == "finished":
            fp = (d.get("info_dict") or {}).get("filepath")
            if fp:
                final["path"] = fp
        on_progress({"status": "postprocess", "pp": d.get("postprocessor"), "pp_status": d["status"]})

    opts = dict(opts, progress_hooks=[phook], postprocessor_hooks=[pphook])
    if logger:
        opts["logger"] = logger
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if info:
        for rd in info.get("requested_downloads") or []:
            if rd.get("filepath"):
                final["path"] = rd["filepath"]
    try:
        os.rmdir(os.path.join(opts["paths"]["home"], ".ytscrape_tmp"))
    except OSError:
        pass
    p = final.get("path")
    return (p if p and os.path.exists(p) else None), (info or {})


# --------------------------------------------------------------------------- #
# Chaining
# --------------------------------------------------------------------------- #
_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def probe(ffmpeg: str, path: str) -> dict:
    """Minimal probe using `ffmpeg -i` (works without ffprobe)."""
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", path], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    err = r.stderr
    m = _DUR_RE.search(err)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    streams = [ln for ln in err.splitlines() if "Stream #" in ln]
    video = [ln for ln in streams if "Video:" in ln and "attached pic" not in ln]
    height = 0
    if video:
        rm = re.search(r"\b(\d{2,5})x(\d{2,5})\b", video[0])
        if rm:
            height = int(rm.group(2))
    return {"duration": dur, "has_video": bool(video), "has_audio": any("Audio:" in s for s in streams),
            "height": height}


def _meta_escape(s: str) -> str:
    return re.sub(r"([=;#\\\n])", r"\\\1", s)


AUDIO_CODEC_ARGS = {
    "mp3": lambda br: ["-c:a", "libmp3lame", "-b:a", f"{br}k"],
    "m4a": lambda br: ["-c:a", "aac", "-b:a", f"{br}k"],
    "opus": lambda br: ["-c:a", "libopus", "-b:a", f"{br}k"],
    "flac": lambda br: ["-c:a", "flac"],
    "wav": lambda br: ["-c:a", "pcm_s16le"],
}


def chain_files(ffmpeg_dir: str, parts: list[tuple[str, str]], out_path: str, mode: str,
                audio_format: str = "mp3", audio_bitrate: str = "192", max_height: int | None = None,
                cancel: threading.Event | None = None,
                on_progress: Callable[[float], None] | None = None,
                log: Callable[[str], None] | None = None) -> tuple[str, str]:
    """Concatenate `parts` [(path, chapter_title)] into `out_path`.

    Re-encodes to a uniform format so any mix of sources joins cleanly, embeds
    one chapter per part, and writes `<out>.timestamps.txt`.
    Returns (out_path, timestamps_text).
    """
    ff = ffmpeg_exe(ffmpeg_dir)
    log = log or (lambda m: None)
    cancel = cancel or threading.Event()
    infos = [probe(ff, p) for p, _ in parts]
    for (p, _), inf in zip(parts, infos):
        if inf["duration"] <= 0:
            raise RuntimeError(f"Could not read duration of {os.path.basename(p)}")

    total = sum(i["duration"] for i in infos)
    stamps = build_timestamps([(t, i["duration"]) for (_, t), i in zip(parts, infos)])
    ts_text = format_timestamps(stamps, total)

    # ffmetadata with chapters
    meta = [";FFMETADATA1", f"title={_meta_escape(Path(out_path).stem)}"]
    for (start, title), inf in zip(stamps, infos):
        meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start * 1000)}",
                 f"END={int((start + inf['duration']) * 1000)}", f"title={_meta_escape(title)}"]
    tmpdir = tempfile.mkdtemp(prefix="ytscrape_chain_")
    meta_path = os.path.join(tmpdir, "chapters.txt")
    Path(meta_path).write_text("\n".join(meta) + "\n", encoding="utf-8")

    args = [ff, "-hide_banner", "-y", "-nostdin"]
    for p, _ in parts:
        args += ["-i", p]
    n = len(parts)
    filters, labels = [], []
    extra = n  # index of next extra lavfi input

    if mode == "video":
        h = max((i["height"] for i in infos if i["has_video"]), default=720) or 720
        if max_height:
            h = min(h, max_height)
        h -= h % 2
        w = int(round(h * 16 / 9 / 2)) * 2
        silent_inputs = []
        for k, inf in enumerate(infos):
            if inf["has_video"]:
                filters.append(f"[{k}:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                               f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v{k}]")
            else:
                filters.append(f"color=c=black:s={w}x{h}:r=30:d={inf['duration']:.3f},format=yuv420p[v{k}]")
            if inf["has_audio"]:
                filters.append(f"[{k}:a:0]aresample=48000,aformat=channel_layouts=stereo[a{k}]")
            else:
                silent_inputs.append((k, inf["duration"]))
                filters.append(f"[{extra}:a]aformat=channel_layouts=stereo[a{k}]")
                extra += 1
            labels.append(f"[v{k}][a{k}]")
        for _, d in silent_inputs:
            args += ["-f", "lavfi", "-t", f"{d:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        filters.append("".join(labels) + f"concat=n={n}:v=1:a=1[vout][aout]")
        maps = ["-map", "[vout]", "-map", "[aout]"]
        codec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "192k"]
        if out_path.lower().endswith(".mp4"):
            codec += ["-movflags", "+faststart"]
    else:
        for k, inf in enumerate(infos):
            if not inf["has_audio"]:
                raise RuntimeError(f"{os.path.basename(parts[k][0])} has no audio track")
            filters.append(f"[{k}:a:0]aresample=48000,aformat=channel_layouts=stereo[a{k}]")
            labels.append(f"[a{k}]")
        filters.append("".join(labels) + f"concat=n={n}:v=0:a=1[aout]")
        maps = ["-map", "[aout]"]
        codec = AUDIO_CODEC_ARGS[audio_format](audio_bitrate)
        if audio_format == "mp3":
            codec += ["-id3v2_version", "3"]

    args += ["-i", meta_path]
    meta_idx = extra
    args += ["-filter_complex", ";".join(filters), *maps, "-map_metadata", str(meta_idx),
             "-map_chapters", str(meta_idx), *codec, "-progress", "pipe:1", "-nostats", out_path]

    log(f"Chaining {n} files → {os.path.basename(out_path)} ({human_duration(total)})")
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    err_tail: list[str] = []

    def read_err():
        for ln in proc.stderr:
            err_tail.append(ln)
            del err_tail[:-40]

    threading.Thread(target=read_err, daemon=True).start()
    for ln in proc.stdout:
        if cancel.is_set():
            proc.kill()
            break
        if ln.startswith("out_time_us=") or ln.startswith("out_time_ms="):
            try:
                us = int(ln.split("=", 1)[1])
                if on_progress and total:
                    on_progress(min(1.0, us / 1e6 / total))
            except ValueError:
                pass
    proc.wait()
    shutil.rmtree(tmpdir, ignore_errors=True)
    if cancel.is_set():
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise Cancelled()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + "".join(err_tail[-15:]))

    ts_file = os.path.splitext(out_path)[0] + ".timestamps.txt"
    Path(ts_file).write_text(ts_text + "\n", encoding="utf-8")
    if on_progress:
        on_progress(1.0)
    return out_path, ts_text


def safe_filename(name: str) -> str:
    name = name.replace(":", " -").replace('"', "'").replace("/", "-").replace("\\", "-").replace("|", "-")
    name = re.sub(r'[<>?*\x00-\x1f]', "", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" .")
    return name[:150] or "untitled"


def open_path(path: str):
    if os.name == "nt":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# --------------------------------------------------------------------------- #
# Timestamps -> chapters -> split files
# --------------------------------------------------------------------------- #
_TS_RE = re.compile(r"(?<![\d:.])(?:(\d{1,2}):)?(\d{1,3}):(\d{2})(?![\d:])")
_EDGE = " \t-–—|:•·>»*~_,;"


def _secs(m: re.Match) -> int:
    h, mi, se = m.group(1), m.group(2), m.group(3)
    return int(h or 0) * 3600 + int(mi) * 60 + int(se)


def parse_timestamps(text: str, duration: float | None = None) -> list[dict]:
    """Read timestamp lines like '00:00 Intro', '1:02:03 - Song', 'Song (3:45)',
    '[03:45] Title', '1. 03:45 Title', '00:00 - 03:45 Song'.

    Returns [{'start', 'end', 'title'}] sorted by start; `end` is None for the
    last one when `duration` is unknown. Needs at least 2 timestamps.
    """
    found = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        matches = list(_TS_RE.finditer(line))
        if not matches:
            continue
        start = _secs(matches[0])
        title = _TS_RE.sub(" ", line)
        title = re.sub(r"[\(\[]\s*[\)\]]", " ", title)          # empty () or [] left behind
        title = re.sub(r"^\s*\d{1,3}\s*[.)]\s+", "", title)       # leading "1. " / "01) "
        title = re.sub(r"\s{2,}", " ", title).strip(_EDGE).strip()
        found.append((start, title))
    if len(found) < 2:
        return []
    found.sort(key=lambda x: x[0])
    chapters, seen = [], set()
    for start, title in found:
        if start in seen or (duration and start >= duration):
            continue
        seen.add(start)
        chapters.append({"start": float(start), "title": title})
    if len(chapters) < 2:
        return []
    if chapters[0]["start"] > 1:
        chapters.insert(0, {"start": 0.0, "title": "Intro"})
    for i, c in enumerate(chapters):
        c["end"] = chapters[i + 1]["start"] if i + 1 < len(chapters) else (float(duration) if duration else None)
        if not c["title"]:
            c["title"] = f"Part {i + 1}"
    return chapters


def chapters_from_info(info: dict) -> list[dict]:
    """Official YouTube chapters, else timestamps parsed from the description."""
    dur = info.get("duration")
    chs = [{"start": float(c.get("start_time") or 0), "end": c.get("end_time"),
            "title": (c.get("title") or "").strip()} for c in (info.get("chapters") or [])]
    if len(chs) >= 2:
        for i, c in enumerate(chs):
            c["title"] = c["title"] or f"Part {i + 1}"
            if c["end"] is None:
                c["end"] = chs[i + 1]["start"] if i + 1 < len(chs) else dur
        return chs
    return parse_timestamps(info.get("description") or "", dur)


def chapters_to_text(chapters: list[dict]) -> str:
    if not chapters:
        return ""
    last = chapters[-1].get("end") or chapters[-1]["start"]
    hours = last >= 3600
    return "\n".join(f"{stamp(c['start'], hours)} {c['title']}" for c in chapters)


SPLIT_NAME_STYLES = ["01 - Title", "Title", "01 - 00.03.45 - Title"]


def _part_name(style: str, i: int, n: int, c: dict) -> str:
    num = f"{i:0{max(2, len(str(n)))}d}"
    title = safe_filename(c["title"])
    if style == "Title":
        return title
    if style.startswith("01 - 00"):
        s = int(c["start"])
        return f"{num} - {s // 3600:02d}.{s % 3600 // 60:02d}.{s % 60:02d} - {title}"
    return f"{num} - {title}"


def split_file(ffmpeg_dir: str, path: str, chapters: list[dict], dest_dir: str, mode: str,
               name_style: str = "01 - Title", meta: dict | None = None,
               cancel: threading.Event | None = None,
               on_progress: Callable[[float], None] | None = None,
               log: Callable[[str], None] | None = None) -> list[str]:
    """Cut `path` into one file per chapter inside `dest_dir`.

    Audio is stream-copied (fast, lossless). Video is re-encoded to H.264/AAC
    so every cut lands exactly on its timestamp instead of the nearest keyframe.
    """
    ff = ffmpeg_exe(ffmpeg_dir)
    log = log or (lambda m: None)
    cancel = cancel or threading.Event()
    meta = meta or {}
    total = probe(ff, path)["duration"]
    os.makedirs(dest_dir, exist_ok=True)
    ext = os.path.splitext(path)[1].lower()
    if mode == "video" and ext == ".webm":
        ext = ".mkv"  # H.264 can't go in WebM
    chs = [dict(c) for c in chapters]
    for i, c in enumerate(chs):
        if c.get("end") is None:
            c["end"] = chs[i + 1]["start"] if i + 1 < len(chs) else total
        if total:
            c["end"] = min(c["end"], total)
    chs = [c for c in chs if c["end"] - c["start"] > 0.05]

    outs, n = [], len(chs)
    for i, c in enumerate(chs, 1):
        if cancel.is_set():
            raise Cancelled()
        out = os.path.join(dest_dir, _part_name(name_style, i, n, c) + ext)
        head = [ff, "-hide_banner", "-y", "-nostdin", "-loglevel", "error",
                "-ss", f"{c['start']:.3f}", "-i", path, "-t", f"{c['end'] - c['start']:.3f}",
                "-map_metadata", "0", "-map_chapters", "-1",
                "-metadata", f"title={c['title']}", "-metadata", f"track={i}/{n}",
                # Ogg/Opus keeps tags on the stream, so set them there too
                "-metadata:s:a:0", f"title={c['title']}", "-metadata:s:a:0", f"track={i}/{n}"]
        if meta.get("album"):
            head += ["-metadata", f"album={meta['album']}", "-metadata:s:a:0", f"album={meta['album']}"]
        if meta.get("artist"):
            head += ["-metadata", f"artist={meta['artist']}", "-metadata", f"album_artist={meta['artist']}",
                     "-metadata:s:a:0", f"artist={meta['artist']}"]
        if mode == "video":
            attempts = [["-map", "0:V:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast",
                         "-crf", "20", "-c:a", "aac", "-b:a", "192k"]
                        + (["-movflags", "+faststart"] if ext == ".mp4" else [])]
        else:
            tail = ["-id3v2_version", "3"] if ext == ".mp3" else []
            attempts = [["-map", "0:a:0", "-map", "0:v?", "-c", "copy"] + tail,  # keep cover art
                        ["-map", "0:a:0", "-c", "copy"] + tail]                   # audio only fallback
        r = None
        for extra in attempts:
            r = subprocess.run(head + extra + [out], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", creationflags=NO_WINDOW)
            if r.returncode == 0:
                break
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg failed on part {i} ({c['title']}):\n{r.stderr[-800:]}")
        outs.append(out)
        log(f"  [{i}/{n}] {os.path.basename(out)}")
        if on_progress:
            on_progress(i / n)
    return outs
