import os
import time
import yt_dlp
import re
from shutil import which



def default_cookiefile():
    cpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
    return cpath if os.path.exists(cpath) else None


def has_ffmpeg():
    return which("ffmpeg") is not None or os.path.exists(r"C:\ffmpeg\bin\ffmpeg.exe")


def build_common_ydl_opts(cookiefile=None):
    # prefer passed cookiefile, else local cookies.txt if exists
    if not cookiefile:
        cookiefile = default_cookiefile()

    opts = {
        "quiet": True,
        "noplaylist": True,
        "ignoreerrors": False,

        # cookies (if file exists we use it; yt-dlp will also write updated jar to it)
        "cookiefile": cookiefile or os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt"),

        # network hardening
        "socket_timeout": 12,
        "retries": 2,
        "fragment_retries": 2,
        "retry_sleep_functions": {
            "http": lambda n: 1,
            "fragment": lambda n: 1,
        },

        # low-RAM / stable
        "concurrent_fragment_downloads": 1,

        # resume
        "continuedl": True,
        "nopart": False,

        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },

        # JS runtime + EJS (YouTube challenge)
        "js_runtimes": {
            "node": {"path": r"C:\Program Files\nodejs\node.exe"},
            "deno": {"path": r"C:\Users\Jamshidbek\.deno\bin\deno.exe"},
        },
        "remote_components": ["ejs:github"],

        # YouTube client order (avoid android)
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "mweb", "tv"],
                "player_skip": ["web_safari"],
            }
        },
    }

    # ffmpeg location if bundled
    if os.path.exists(r"C:\ffmpeg\bin\ffmpeg.exe"):
        opts["ffmpeg_location"] = r"C:\ffmpeg\bin"

    return opts


class DownloadCancelled(Exception):
    pass


class YTDLJobRunner:
    """
    One job runner per card (thread). Updates UI through callbacks.
    """
    def __init__(self, url, media_type, quality_value, download_dir,
                 on_progress, on_state, pause_event, cancel_event, cookiefile=None, subtitle_lang: str = ""):
        self.url = url
        self.media_type = media_type  # Video / Audio / Image
        self.quality_value = quality_value  # e.g. "1080p" or "192kbps"
        self.download_dir = download_dir
        self.on_progress = on_progress
        self.on_state = on_state
        self.pause_event = pause_event
        self.cancel_event = cancel_event
        self.cookiefile = cookiefile
        self.subtitle_lang = (subtitle_lang or "").strip()

        self._last_bytes = None
        self._last_time = None

    def _hook(self, d):
        if self.cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled() # type: ignore

        # Pause: wait safely without corrupting
        while self.pause_event.is_set():
            time.sleep(0.15)
            if self.cancel_event.is_set():
                raise yt_dlp.utils.DownloadCancelled() # type: ignore

        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            pct = (downloaded / total) if total else 0.0

            speed = d.get("speed")  # bytes/sec
            eta = d.get("eta")      # seconds

            # Fallback speed estimation if not provided
            now = time.time()
            if speed is None:
                if self._last_bytes is not None and self._last_time is not None:
                    dt = max(now - self._last_time, 1e-6)
                    speed = max(downloaded - self._last_bytes, 0) / dt
                self._last_bytes = downloaded
                self._last_time = now

            self.on_progress(pct, speed, eta)

        elif status == "finished":
            self.on_state("processing")

    def run(self, filename_template="%(title)s"):
        os.makedirs(self.download_dir, exist_ok=True)

        fmt = "best"
        postprocessors = None
        merge_output_format = None

        if self.media_type == "Video":
            m = re.search(r"(\d{3,4})p", str(self.quality_value).lower())
            height = int(m.group(1)) if m else None

            if height:
                # Prefer MP4 (H.264 avc1 + m4a)
                fmt = (
                    f"bestvideo[height={height}][vcodec^=avc1][ext=mp4]+bestaudio[acodec^=mp4a]/"
                    f"bestvideo[height<={height}][vcodec^=avc1][ext=mp4]+bestaudio[acodec^=mp4a]/"
                    f"best[height<={height}][ext=mp4]/"
                    f"best[height<={height}]/best"
                )
            else:
                fmt = "best[height<=720][ext=mp4]/best[height<=720]/best"

            merge_output_format = "mp4"

        elif self.media_type == "Audio":
            if not has_ffmpeg():
                raise RuntimeError("FFmpeg not found in PATH. Install FFmpeg to extract MP3.")

            try:
                abr = int(str(self.quality_value).lower().replace("kbps", "").strip())
            except Exception:
                abr = None

            # Strict: NEVER pick video streams
            if abr:
                fmt = (
                    f"bestaudio[abr>={abr}][vcodec=none]/"
                    f"bestaudio[vcodec=none]/"
                    f"best[vcodec=none][abr>={abr}]/"
                    f"best[vcodec=none]"
                )
            else:
                fmt = "bestaudio[vcodec=none]/best[vcodec=none]"

            ydl_audio_pp = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(abr or 192),
            }]

            postprocessors = ydl_audio_pp

        outtmpl = os.path.join(
            self.download_dir,
            f"{filename_template}-{self.quality_value}.%(ext)s"
        )

        # ✅ ydl_opts MUST be defined before try (fixes UnboundLocalError)
        ydl_opts = build_common_ydl_opts(cookiefile=self.cookiefile)
        ydl_opts.update({
            "format": fmt,
            "outtmpl": outtmpl,
            "progress_hooks": [self._hook],
        })

        # ===== subtitles (Video only) =====
        sub = self.subtitle_lang
        if self.media_type == "Video" and sub:
            is_auto = sub.endswith(":auto")
            lang = sub.split(":", 1)[0].strip()

            # write subtitle file
            ydl_opts["ignoreerrors"] = False
            ydl_opts["writesubtitles"] = (not is_auto)
            ydl_opts["writeautomaticsub"] = bool(is_auto)
            ydl_opts["subtitleslangs"] = [lang]
            ydl_opts["subtitlesformat"] = "best"

            # optional: embed subs into container (requires ffmpeg)
            ydl_opts["embedsubtitles"] = True


        # audio-only extras
        if self.media_type == "Audio":
            ydl_opts.update({
                "format_sort": ["+abr", "+asr", "+size"],
            })

        if merge_output_format:
            ydl_opts["merge_output_format"] = merge_output_format
        if postprocessors:
            ydl_opts["postprocessors"] = postprocessors

        self.on_state("downloading")

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl: # type: ignore
                ydl.download([self.url])

        except Exception as e:
            msg = str(e).lower()
            if "403" in msg or "forbidden" in msg:
                # ✅ correct key path (your previous version was wrong)
                ydl_opts.setdefault("extractor_args", {}).setdefault("youtube", {})
                ydl_opts["extractor_args"]["youtube"]["player_client"] = ["tv", "mweb", "web"]

                with yt_dlp.YoutubeDL(ydl_opts) as ydl: # type: ignore
                    ydl.download([self.url])
            if "subtitle" in msg and ("not available" in msg or "requested" in msg):
                # retry without subtitles
                ydl_opts.pop("writesubtitles", None)
                ydl_opts.pop("writeautomaticsub", None)
                ydl_opts.pop("subtitleslangs", None)
                ydl_opts.pop("subtitlesformat", None)
                with yt_dlp.YoutubeDL(ydl_opts) as ydl: # type: ignore
                    ydl.download([self.url])
            else:
                raise

        self.on_state("completed")


def extract_formats(url: str, cookiefile=None):
    ydl_opts = build_common_ydl_opts(cookiefile=cookiefile)
    ydl_opts["skip_download"] = True

    with yt_dlp.YoutubeDL(ydl_opts) as ydl: # type: ignore
        info = ydl.extract_info(url, download=False)

    thumb = info.get("thumbnail")  # yt-dlp beradi
    title = info.get("title") or "Unknown"
    formats = info.get("formats", [])
    
    #adding subtitle
    subs_out = []
    subs_map = info.get("subtitles") or {}
    auto_map = info.get("automatic_captions") or {}

    # all keys (manual + auto), duplicate bo‘lsa bir marta
    lang_keys = list(subs_map.keys())
    for k in auto_map.keys():
        if k not in subs_map:
            lang_keys.append(k)

    # normalize -> [(display, value)]
    # value: "en" yoki "en:auto" ko‘rinishida
    for lang in lang_keys:
        is_auto = (lang not in subs_map) and (lang in auto_map)
        tag = "auto" if is_auto else "sub"
        display = f"{lang} ({tag})"
        value = f"{lang}:auto" if is_auto else lang
        subs_out.append((display, value))

    # "None" variant doim bo‘lsin
    subs_out.insert(0, ("None", ""))
    #adding subtitle finished

    # audio candidates
    audio_candidates = []
    for f in formats: # type: ignore
        if f.get("acodec") != "none" and f.get("vcodec") == "none":
            abr = f.get("abr") or 0
            size = f.get("filesize") or f.get("filesize_approx") or 0
            audio_candidates.append((abr, size, f))

    audio_candidates.sort(key=lambda x: (x[0] or 0), reverse=True)

    best_audio = None
    for abr, size, f in audio_candidates:
        if abr and abr <= 160:
            best_audio = f
            break
    if best_audio is None and audio_candidates:
        best_audio = audio_candidates[0][2]

    best_audio_size = (best_audio.get("filesize") or best_audio.get("filesize_approx") or 0) if best_audio else 0

    # height fallback parser
    

    def _get_height(f):
        h = f.get("height")
        if h:
            return int(h)

        res = f.get("resolution")
        if isinstance(res, str):
            m = re.search(r"x(\d{3,4})", res)
            if m:
                return int(m.group(1))

        note = f.get("format_note")
        if isinstance(note, str):
            m = re.search(r"(\d{3,4})p", note.lower())
            if m:
                return int(m.group(1))

        fmt_s = f.get("format")
        if isinstance(fmt_s, str):
            m = re.search(r"(\d{3,4})p", fmt_s.lower())
            if m:
                return int(m.group(1))

        return None

    # pick best per height (prefer higher tbr)
    best_video_by_height = {}
    for f in formats: # type: ignore
        if f.get("vcodec") != "none":
            h = _get_height(f)
            if not h:
                continue
            tbr = f.get("tbr") or 0
            cur = best_video_by_height.get(h)
            if cur is None or (tbr > (cur.get("tbr") or 0)):
                best_video_by_height[h] = f

    video_items = []
    for h in sorted(best_video_by_height.keys(), reverse=True):
        vf = best_video_by_height[h]
        vsize = vf.get("filesize") or vf.get("filesize_approx") or 0
        total_bytes = vsize + best_audio_size if vsize else 0

        if total_bytes:
            mb = total_bytes / (1024 * 1024)
            display = f"{h}p (~{mb:.0f} MB)"
        else:
            display = f"{h}p"

        value = f"{h}p"
        video_items.append((display, value))

    # audio bitrates list
    audio_items = []
    seen = set()
    for abr, size, f in audio_candidates:
        if not abr:
            continue
        abr = int(abr)
        if abr in seen:
            continue
        seen.add(abr)

        if size:
            mb = size / (1024 * 1024)
            display = f"{abr}kbps (~{mb:.0f} MB)"
        else:
            display = f"{abr}kbps"
        value = f"{abr}kbps"
        audio_items.append((display, value))

    audio_items.sort(key=lambda x: int(x[1].replace("kbps", "")), reverse=True)

    return {
        "title": title,
        "thumbnail": thumb,
        "video_resolutions": video_items,  # list of (display,value)
        "audio_bitrates": audio_items,     # list of (display,value)
        "has_image": False,
        "subtitles": subs_out,   # <-- NEW
    }


def extract_subtitles_only(url: str, cookiefile=None):
    ydl_opts = build_common_ydl_opts(cookiefile=cookiefile)
    ydl_opts.update({
        "skip_download": True,
        "quiet": True,
        "noplaylist": True,
    })

    with yt_dlp.YoutubeDL(ydl_opts) as ydl: # type: ignore
        info = ydl.extract_info(url, download=False)

    subs_out = []
    subs_map = info.get("subtitles") or {}
    auto_map = info.get("automatic_captions") or {}

    lang_keys = list(subs_map.keys())
    for k in auto_map.keys():
        if k not in subs_map:
            lang_keys.append(k)

    for lang in lang_keys:
        is_auto = (lang not in subs_map) and (lang in auto_map)
        tag = "auto" if is_auto else "sub"
        display = f"{lang} ({tag})"
        value = f"{lang}:auto" if is_auto else lang
        subs_out.append((display, value))

    subs_out.insert(0, ("None", ""))
    return subs_out
