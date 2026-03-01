import os
import json
import datetime
import threading
import time
import subprocess
import tkinter as tk
import customtkinter as ctk
from tkinter import filedialog

from downloader_engine import extract_formats, YTDLJobRunner
from persistence import dedupe_history, upsert_history, load_history, delete_history_item
from bgutil_manager import start_bgutil_if_needed, stop_bgutil

from PIL import Image
from io import BytesIO
import requests

_THUMB_CACHE = {}        # thumb_url -> CTkImage
_THUMB_BYTES_CACHE = {}  # thumb_url -> raw bytes (ixtiyoriy)


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


def _load_thumb_bytes(url: str, timeout=8):
    # cache bytes
    b = _THUMB_BYTES_CACHE.get(url)
    if b:
        return b
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    b = r.content
    _THUMB_BYTES_CACHE[url] = b
    return b

def _make_ctk_thumb_from_bytes(b: bytes, size=(140, 78)):
    img = Image.open(BytesIO(b)).convert("RGB")
    img.thumbnail(size, Image.Resampling.LANCZOS)
    return ctk.CTkImage(light_image=img, dark_image=img, size=img.size)




# ==================== SETTINGS MANAGER ====================
class SettingsManager:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    FILE = os.path.join(BASE_DIR, "settings.json")


    DEFAULTS = {
        "theme": "dark",
        "cookies_mode": "off",          # off | file | browser
        "cookie_file": "",
        "cookies_browser": "chrome",    # chrome | edge | firefox
        "cookies_profile": "",          # optional
        "accent": "blue",
        "text_scale": 1.0,
        "download_dir": "Downloads",
        "stats": {
            "total": 0,
            "completed": 0,
            "failed": 0,
            "last_size": 0,
            "history": []
        }
    }

    def __init__(self):
        self.data = {}
        self.load()
        self.apply_all()

    def load(self):
        if not os.path.exists(self.FILE):
            self.data = json.loads(json.dumps(self.DEFAULTS))
            self.save()
            return
        with open(self.FILE, "r", encoding="utf-8") as f:
            self.data = json.load(f)
        for k, v in self.DEFAULTS.items():
            self.data.setdefault(k, v)
        for k, v in self.DEFAULTS["stats"].items():
            self.data["stats"].setdefault(k, v)

    def save(self, updates=None):
        if updates:
            self.data.update(updates)
        with open(self.FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=4)

    def apply_all(self):
        ctk.set_appearance_mode(self.data.get("theme", "dark"))
        ctk.set_default_color_theme(self.data.get("accent", "blue"))
        ctk.set_widget_scaling(self.data.get("text_scale", 1.0))
        os.makedirs(self.data.get("download_dir", "Downloads"), exist_ok=True)

    def reset(self):
        self.data = json.loads(json.dumps(self.DEFAULTS))
        self.save()
        self.apply_all()

    def update_stats(self, total=None, completed=None, failed=None, last_size=None):
        stats = self.data.get("stats", {})
        if total is not None:
            stats["total"] = total
        if completed is not None:
            stats["completed"] = completed
        if failed is not None:
            stats["failed"] = failed
        if last_size is not None:
            stats["last_size"] = last_size

        today = datetime.date.today().strftime("%Y-%m-%d")
        stats["history"].append({"date": today, "total": stats["total"]})
        stats["history"] = stats["history"][-30:]
        self.data["stats"] = stats
        self.save()


# ==================== SIDEBAR ====================
class Sidebar(ctk.CTkFrame):
    """
    - Minimal, stable, modern
    - Active state via 1 function
    - Hover effect (desktop-friendly)
    - Safe if unknown page key
    """
    ITEMS = (
        ("Dashboard", "dashboard"),
        ("Downloader", "downloader"),
        ("History", "history"),
        ("Settings", "settings"),
    )

    def __init__(self, parent, page_callback, default_page="downloader"):
        super().__init__(parent, corner_radius=0)
        self.page_callback = page_callback
        self.buttons: dict[str, ctk.CTkButton] = {}
        self.active_page: str | None = None

        # theme-ish colors (CTk supports tuple for light/dark)
        self._c_bg = "transparent"
        self._c_hover = ("gray88", "gray22")
        self._c_active = ("gray80", "gray25")
        self._c_txt = ("gray10", "gray90")
        self._c_txt_active = ("black", "white")

        self._build()
        self.set_active(default_page)

    def _build(self):
        # Header
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(fill="x", padx=12, pady=(16, 10))

        ctk.CTkLabel(hdr, text="MENU", font=("Arial", 16, "bold")).pack(anchor="w")

        # Items
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=8, pady=(0, 10))

        for text, key in self.ITEMS:
            btn = ctk.CTkButton(
                body,
                text=text,
                fg_color=self._c_bg,
                hover_color=self._c_hover,
                text_color=self._c_txt,
                corner_radius=10,
                height=36,
                anchor="w",
                command=lambda k=key: self.on_click(k),
            )
            btn.pack(fill="x", padx=4, pady=4)

            # Optional: subtle left padding
            btn.configure(text="   " + text)

            self._bind_hover(btn, key)
            self.buttons[key] = btn

    def _bind_hover(self, btn: ctk.CTkButton, key: str):
        # Keep hover feel, but don't override active styling
        def on_enter(_e=None):
            if self.active_page != key:
                btn.configure(fg_color=self._c_hover)

        def on_leave(_e=None):
            if self.active_page != key:
                btn.configure(fg_color=self._c_bg)

        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)

    def on_click(self, page: str):
        self.set_active(page)
        self.page_callback(page)

    def set_active(self, page: str):
        if page not in self.buttons:
            return  # unknown page key, ignore safely

        # reset old
        if self.active_page and self.active_page in self.buttons:
            old = self.buttons[self.active_page]
            old.configure(fg_color=self._c_bg, text_color=self._c_txt)

        # set new
        cur = self.buttons[page]
        cur.configure(fg_color=self._c_active, text_color=self._c_txt_active)
        self.active_page = page



# ==================== CENTER TOP BAR ====================
class CenterTopBar(ctk.CTkFrame):
    def __init__(self, parent, toggle_callback):
        super().__init__(parent, height=50, corner_radius=0)
        self.grid(row=0, column=0, sticky="nsew")
        self.grid_propagate(False)
        ctk.CTkButton(self, text="☰", width=40, command=toggle_callback).pack(side="left", padx=10)
        ctk.CTkLabel(self, text="Media Downloader", font=("Arial", 18, "bold")).pack(side="left", padx=10)


# ==================== PAGES ====================
class BasePage(ctk.CTkFrame):
    def __init__(self, parent, title):
        super().__init__(parent)
        ctk.CTkLabel(self, text=title, font=("Arial", 24)).pack(pady=40)


# ---- Dashboard Page ----
class DashboardPage(ctk.CTkFrame):
    def __init__(self, parent, settings: SettingsManager):
        super().__init__(parent)
        self.settings = settings
        pass



class ManualFormatCard(ctk.CTkFrame):
    """
    Layout:
    [ thumb ] [ title/url ]
            [ quality box (buttons wrap responsively) ]
    """
    def __init__(self, parent, url, title, media_type, qualities, start_cb, thumbnail_url=None, subtitles=None):
        super().__init__(parent, corner_radius=12)

        self.url = url
        self.title = title
        self.media_type = media_type
        self.qualities = qualities or []
        self.start_cb = start_cb
        self.thumbnail_url = (thumbnail_url or "").strip()
        self.subtitles = subtitles or [("None", "")]
        self.sub_var = ctk.StringVar(value="")  # "" => None
        self.started = False
        self._btns = []
        self._reflow_after = None

        PAD_X = 10
        PAD_Y = 10

        # ===== GRID (2 columns) =====
        self.grid_columnconfigure(0, minsize=180)
        self.grid_columnconfigure(1, weight=1)

        # ===== LEFT: THUMB =====
        thumb_wrap = ctk.CTkFrame(self, corner_radius=10)
        thumb_wrap.grid(row=0, column=0, rowspan=3, sticky="nw", padx=PAD_X, pady=PAD_Y)

        self.thumb_label = ctk.CTkLabel(thumb_wrap, text="", width=160, height=90)
        self.thumb_label.pack(padx=6, pady=(6, 2))

        self.type_chip = ctk.CTkLabel(
            thumb_wrap,
            text=self.media_type.upper(),
            corner_radius=8,
            padx=10, pady=3
        )
        self.type_chip.pack(padx=6, pady=(0, 6), anchor="w")

        # ===== RIGHT: HEADER =====
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=1, sticky="ew", padx=(0, PAD_X), pady=(PAD_Y, 6))
        header.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(
            header,
            text=title,
            font=("Arial", 15, "bold"),
            justify="left",
            anchor="w"
        )
        self.title_label.grid(row=0, column=0, sticky="ew")

        self.url_label = ctk.CTkLabel(
            header,
            text=url,
            text_color="gray",
            justify="left",
            anchor="w"
        )
        self.url_label.grid(row=1, column=0, sticky="ew", pady=(2, 0))

        # ===== RIGHT: QUALITY CARD =====
        self.qcard = ctk.CTkFrame(self, corner_radius=12)
        self.qcard.grid(row=1, column=1, sticky="nsew", padx=(0, PAD_X), pady=(0, 6))
        # hint row (row=0) ichida: chapda hint, o‘ngda subtitle menu
        self.qcard.grid_columnconfigure(0, weight=1)
        self.qcard.grid_columnconfigure(1, weight=0)

        self.hint = ctk.CTkLabel(self.qcard, text="Pick a quality to start download:", text_color="orange")
        self.hint.grid(row=0, column=0, sticky="w", padx=10, pady=(0, 0))

        # Subtitle menu faqat video bo‘lsa aktiv
        sub_values = [d for (d, v) in self.subtitles] or ["None"]
        self._sub_display_to_value = {d: v for (d, v) in self.subtitles}

        self.sub_var = ctk.StringVar(value=sub_values[0])

        self.sub_menu = ScrollableOptionMenu(
            self.qcard,
            values=sub_values,
            variable=self.sub_var,
            command=self._on_subtitle_change,
            width=180,
            max_visible=10
        )

        self.sub_menu.grid(row=0, column=1, sticky="e", padx=10, pady=(0, 0))
        # default = None
        self.sub_var.set("None")

        if self.media_type != "Video":
            self.sub_menu.configure(state="disabled")


        self.grid_wrap = ctk.CTkFrame(self.qcard, fg_color="transparent")
        self.grid_wrap.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 8))
        self.grid_wrap.grid_columnconfigure(0, weight=1)

        self.btn_grid = ctk.CTkFrame(self.grid_wrap, fg_color="transparent")
        self.btn_grid.grid(row=0, column=0, sticky="ew")
        self.btn_grid.bind("<Configure>", self._on_grid_configure)

        # build buttons once
        if not self.qualities:
            ctk.CTkLabel(self.btn_grid, text="No formats found.", text_color="red").grid(
                row=0, column=0, sticky="w", padx=2, pady=2
            )
        else:
            for item in self.qualities:
                if isinstance(item, tuple):
                    display_text, value = item
                else:
                    display_text, value = item, item

                btn = ctk.CTkButton(
                    self.btn_grid,
                    text=display_text,
                    height=30,
                    command=lambda vv=value: self._choose_quality(vv)
                )
                self._btns.append(btn)

            self._reflow_buttons()  # initial layout

        self._start_thumb_async()

    def _on_subtitle_change(self, display_label):
        self.selected_subtitle = self._sub_display_to_value.get(display_label, "")

    # ---------- responsive button wrap ----------
    def _on_grid_configure(self, _evt=None):
        # debounce to avoid constant reflow while resizing
        if self._reflow_after:
            try:
                self.after_cancel(self._reflow_after)
            except Exception:
                pass
        self._reflow_after = self.after(60, self._reflow_buttons)

    def _reflow_buttons(self):
        self._reflow_after = None
        if not self._btns or not self.winfo_exists():
            return

        # available width inside btn_grid
        w = max(1, self.btn_grid.winfo_width())

        # tune these for your UI density
        BTN_MIN_W = 120
        PAD = 5
        CELL_W = BTN_MIN_W + (PAD * 2)

        cols = max(1, w // CELL_W)
        cols = min(cols, 6)  # avoid silly-wide grids

        # clear old placements
        for b in self._btns:
            b.grid_forget()

        # configure columns to expand so buttons fill full card width
        # first reset a reasonable range
        for c in range(0, 12):
            self.btn_grid.grid_columnconfigure(c, weight=0, uniform="")
        for c in range(cols):
            self.btn_grid.grid_columnconfigure(c, weight=1, uniform="qcols")

        # place buttons
        r = 0
        c = 0
        for b in self._btns:
            b.grid(row=r, column=c, padx=PAD, pady=PAD, sticky="ew")
            c += 1
            if c >= cols:
                c = 0
                r += 1

    # ---------- actions ----------
    def _choose_quality(self, quality):
        if self.started:
            return
        self.started = True

        disp = self.sub_var.get()  # ✅ doim display label
        sub_val = self._sub_display_to_value.get(disp, "")

        self.hint.configure(text=f"Starting: {quality} ...", text_color="orange")
        self.start_cb(self, self.url, quality, sub_val)  # <-- NEW signature

    # ---------- thumbnail async (sening caching funksiyalaringga mos) ----------
    def _start_thumb_async(self):
        url = self.thumbnail_url
        if not url:
            self.thumb_label.configure(text="No\nthumb", text_color="gray")
            return

        cached = _THUMB_CACHE.get(url)
        if cached:
            self._apply_thumb(cached)
            return

        def worker():
            try:
                b = _load_thumb_bytes(url)
                img = _make_ctk_thumb_from_bytes(b, size=(160, 90))
                _THUMB_CACHE[url] = img
                if self.winfo_exists():
                    self.after(0, lambda: self._apply_thumb(img))
            except Exception:
                if self.winfo_exists():
                    self.after(0, lambda: self.thumb_label.configure(text="Thumb\nerror", text_color="red"))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_thumb(self, img):
        if not self.winfo_exists():
            return
        self.thumb_label.configure(image=img, text="")





# ==================== DOWNLOAD CARD (REAL YT-DLP) ====================
class DownloadCard(ctk.CTkFrame):
    def __init__(self, parent, title, link, media_type, quality, settings: SettingsManager,
                 dashboard: DashboardPage | None = None, history_page=None, thumbnail_url=None, subtitle_lang=""):
        super().__init__(parent, corner_radius=10, fg_color="#222")

        self.parent = parent
        self.title = title
        self.link = link
        self.media_type = media_type
        self.quality = quality
        self.settings = settings
        self.dashboard = dashboard
        self.history_page = history_page
        self.thumbnail_url = (thumbnail_url or "").strip()
        self.subtitle_lang = (subtitle_lang or "").strip()
        self._last_progress_ts = time.time()
        self._stall_after_id = None



        self.status = "queued"
        self.progress_value = 0.0

        self.pause_event = threading.Event()
        self.cancel_event = threading.Event()


        # COMPACT layout: eni saqlanadi, bo‘yi kichrayadi
        # - thumbnail 160x90 qoladi
        # - title/link/info bitta qatorda (2 qator emas)
        # - progress + buttons bitta pastki qatorda
        # - umumiy height kamayadi

        self.grid_columnconfigure(1, weight=1)

        # ======= LEFT: THUMB =======
        thumb_wrap = ctk.CTkFrame(self, corner_radius=10)
        thumb_wrap.grid(row=0, column=0, rowspan=2, sticky="nw", padx=10, pady=10)

        self.thumb_label = ctk.CTkLabel(thumb_wrap, text="", width=160, height=90)
        self.thumb_label.pack(padx=6, pady=6)

        # Media type chip (overlay-like: thumb ostida emas, ichida pastga yopishtirilgan)
        self.type_chip = ctk.CTkLabel(
            thumb_wrap,
            text=self.media_type.upper(),
            corner_radius=8,
            padx=8, pady=2
        )
        self.type_chip.place(relx=0.06, rely=0.78)  # compact overlay

        # ======= RIGHT: TOP META (three row) =======
        self.meta = ctk.CTkFrame(self, fg_color="transparent")
        self.meta.grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=(0, 0))
        self.meta.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(self.meta, text=title, font=("Arial", 14, "bold"))
        self.title_label.grid(row=0, column=0, sticky="w", pady=(0, 0))

        # info yonma-yon: title o‘ngida
        self.info_label = ctk.CTkLabel(self.meta, text="ETA: -- | Speed: --", text_color="orange")
        self.info_label.grid(row=0, column=1, sticky="e", padx=(12, 0), pady=(0, 0))

        # ======= RIGHT: LINK (single line) =======
        self.link_label = ctk.CTkLabel(self.meta, text=link, text_color="gray", anchor="w")
        self.link_label.grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(0, 0))

        self.progress = ctk.CTkProgressBar(self.meta, height=10)
        self.progress.set(0)
        self.progress.grid(row=1, column=1, sticky="e", padx=(12, 0))

        btn_frame = ctk.CTkFrame(self.meta, fg_color="transparent")
        btn_frame.grid(row=2, column=1, sticky="e", pady=(10, 0))


        # tugmalarni ham biroz ixcham qilamiz (width 62)
        self.pause_btn = ctk.CTkButton(btn_frame, text="Pause", width=62, height=28, command=self.pause)
        self.resume_btn = ctk.CTkButton(btn_frame, text="Resume", width=62, height=28, command=self.resume)
        self.retry_btn = ctk.CTkButton(btn_frame, text="Retry", width=62, height=28, command=self.retry)
        self.cancel_btn = ctk.CTkButton(btn_frame, text="Cancel", width=62, height=28, command=self.cancel)

        self.pause_btn.pack(side="left", padx=4)
        self.resume_btn.pack(side="left", padx=4)
        self.retry_btn.pack(side="left", padx=4)
        self.cancel_btn.pack(side="left", padx=(4, 0))


        
        self.msg_label = None
        self._thread = None
        self._last_ui_prog_ts = 0.0
        self._pending_prog = ""
        self._prog_after_id = None
        self._error_text = None

        # thumbnail async
        self._start_thumb_async()

        self.start_download()
    
    def update_meta(self, title: str = "", thumbnail_url: str = ""):
        if not self.winfo_exists():
            return

        if title:
            self.title = title
            self.title_label.configure(text=title)

        if thumbnail_url and thumbnail_url != self.thumbnail_url:
            self.thumbnail_url = thumbnail_url
            # eski image qolib ketmasin (ixtiyoriy)
            try:
                self.thumb_label.configure(text="", image=None)
            except Exception:
                pass
            # qayta yuklash
            self._start_thumb_async()


    # ======= thumbnail loader (sen ishlatgan variantga mos) =======
    def _start_thumb_async(self):
        url = self.thumbnail_url
        if not url:
            self.thumb_label.configure(text="No\nthumb", text_color="gray")
            return

        cached = _THUMB_CACHE.get(url)
        if cached:
            self._apply_thumb(cached)
            return

        def worker():
            try:
                b = _load_thumb_bytes(url)
                img = _make_ctk_thumb_from_bytes(b, size=(160, 90))
                _THUMB_CACHE[url] = img
                if self.winfo_exists():
                    self.after(0, lambda: self._apply_thumb(img))
            except Exception:
                if self.winfo_exists():
                    self.after(0, lambda: self.thumb_label.configure(text="Thumb\nerror", text_color="red"))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_thumb(self, img):
        if not self.winfo_exists():
            return
        self.thumb_label.configure(image=img, text="")
        

    def stop(self):
        # DownloaderPage.stop_all() calls .stop()
        self.cancel()

    def _set_message(self, text, color):
        if not self.winfo_exists() or getattr(self.winfo_toplevel(), "is_closing", False):
            return
        try:
            if self.msg_label is None:
                self.msg_label = ctk.CTkLabel(self.meta, text=text, text_color=color)
                self.msg_label.grid(row=2, column=0, sticky="w", pady=(5, 0))
            else:
                if self.msg_label.winfo_exists():
                    self.msg_label.configure(text=text, text_color=color)
        except Exception:
            pass


    def _format_speed(self, bps):
        if not bps:
            return "--"
        kb = bps / 1024
        if kb < 1024:
            return f"{kb:.0f} KB/s"
        return f"{kb/1024:.2f} MB/s"

    def _format_eta(self, eta):
        if eta is None:
            return "--"
        if eta < 60:
            return f"{int(eta)}s"
        return f"{int(eta//60)}m {int(eta%60)}s"

    def _flush_progress_ui(self):
        self._prog_after_id = None
        data = self._pending_prog
        
        if not data:
            return
            
        pct, speed, eta = data  # Endi Pylance "Never" deb o'ylamaydi
        self.progress_value = pct
        self.progress.set(self.progress_value)
        self.info_label.configure(text=f"ETA: {self._format_eta(eta)} | Speed: {self._format_speed(speed)}")



    def start_download(self):
        # start_download() boshida
        cookiefile = None
        if self.settings.data.get("cookies_mode") == "file":
            cookiefile = (self.settings.data.get("cookie_file") or "").strip() or None
        # Reset UI state
        self.status = "downloading"
        self.progress.set(0)
        self.progress_value = 0.0
        self._error_text = None
        self._set_message("⏳ Downloading...", "orange")

        self._last_progress_ts = time.time()

        if self._stall_after_id is not None:
            try: self.after_cancel(self._stall_after_id)
            except Exception: pass
            self._stall_after_id = None

        def _watch_stall():
            self._stall_after_id = None

            if not self.winfo_exists() or getattr(self.winfo_toplevel(), "is_closing", False):
                return
            if self.status != "downloading":
                return
            if self.pause_event.is_set() or self.cancel_event.is_set():
                return

            # 20-30s: real uzilish/stall uchun yetarli threshold
            if time.time() - self._last_progress_ts > 25:
                # internet uzildi yoki yt-dlp stuck: jobni majburan yakunla
                self.cancel_event.set()
                self.status = "error"
                self._error_text = "Network stalled / connection lost"

                try:
                    self._set_message(f"⚠ Error: {self._error_text}", "red")
                    self.info_label.configure(text="ETA: -- | Speed: --")
                    self.progress.set(self.progress_value)  # yoki 0
                except Exception:
                    pass

                return

            # davomiy monitoring
            self._stall_after_id = self.after(1000, _watch_stall)

        self._stall_after_id = self.after(3000, _watch_stall)


        # Stats total ++ once per card start
        stats = self.settings.data["stats"]
        self.settings.update_stats(total=stats.get("total", 0) + 1)

        download_dir = self.settings.data.get("download_dir", "Downloads")

        def on_progress(pct, speed, eta):
            if self.cancel_event.is_set() or self.status in ("cancelled", "error"):
                return

            self._last_progress_ts = time.time()

            if not self.winfo_exists() or getattr(self.winfo_toplevel(), "is_closing", False):
                return

            # widget yo‘q bo‘lsa, hech narsa qilma
            if not self.winfo_exists() or getattr(self, "status", "") == "cancelled":
                return

            pct = max(0.0, min(1.0, float(pct or 0.0)))
            self._pending_prog = (pct, speed, eta)

            now = time.time()
            if now - self._last_ui_prog_ts < 0.12:
                if self._prog_after_id is None and self.winfo_exists():
                    try:
                        if not self.winfo_exists() or getattr(self.winfo_toplevel(), "is_closing", False):
                            return

                        self._prog_after_id = self.after(120, self._flush_progress_ui)
                    except Exception:
                        self._prog_after_id = None
                return

            self._last_ui_prog_ts = now
            try:
                if not self.winfo_exists() or getattr(self.winfo_toplevel(), "is_closing", False):
                    return

                self.after(0, self._flush_progress_ui)
            except Exception:
                pass



        def on_state(state):
            if self.cancel_event.is_set() or self.status in ("cancelled", "error"):
                return

            self._last_progress_ts = time.time()

            if not self.winfo_exists() or getattr(self.winfo_toplevel(), "is_closing", False):
                return

            if not self.winfo_exists() or getattr(self, "status", "") == "cancelled":
                return
            def ui_state():
                if not self.winfo_exists():
                    return
                if state == "processing":
                    self._set_message("🔧 Processing...", "orange")
                elif state == "completed":
                    self._set_message("✔ Completed", "green")
                elif state == "downloading":
                    self._set_message("⏳ Downloading...", "orange")
            try:
                self.after(0, ui_state)
            except Exception:
                pass


        def run():
            try:
                runner = YTDLJobRunner(
                    url=self.link,
                    media_type=self.media_type,
                    quality_value=self.quality,
                    download_dir=download_dir,
                    on_progress=on_progress,
                    on_state=on_state,
                    pause_event=self.pause_event,
                    cancel_event=self.cancel_event,
                    cookiefile=cookiefile,  # ✅ add this
                    subtitle_lang=self.subtitle_lang,   # <-- NEW
                )
                runner.run(filename_template="%(title)s")

                if self.cancel_event.is_set() or self.status in ("cancelled", "error"):
                    return


                self.status = "completed"
                if self.winfo_exists():
                    self.after(0, self._finish_buttons)

                # update stats
                stats = self.settings.data["stats"]
                completed = stats.get("completed", 0) + 1
                self.settings.update_stats(completed=completed)

                upsert_history(
                    url=self.link,
                    type_=self.media_type,
                    title=self.title,                 # or use extracted title if you have it
                    last_status="completed",
                    quality=self.quality,
                    error=""
                )


                # refresh dashboard/history
                # if self.dashboard:
                #     self.after(0, self.dashboard.update_from_json)
                if self.history_page:
                    self.after(0, self.history_page.refresh)

            except Exception as e:
                # CANCEL bo'lsa jim chiq
                if self.cancel_event.is_set() or self.status == "cancelled":
                    return
                self.status = "error"
                self._error_text = str(e)

                def ui_err():
                    if not self.winfo_exists() or getattr(self.winfo_toplevel(), "is_closing", False):
                        return
                    self._set_message(f"⚠ Error: {self._error_text}", "red")
                    # update failed stats
                    stats = self.settings.data["stats"]
                    failed = stats.get("failed", 0) + 1
                    self.settings.update_stats(failed=failed)

                    upsert_history(
                        url=self.link,
                        type_=self.media_type,
                        title=self.title,
                        last_status="failed",
                        quality=self.quality,
                        error=self._error_text
                    )


                    # if self.dashboard:
                    #     self.dashboard.update_from_json()
                    if self.history_page:
                        self.history_page.refresh()

                try:
                    if self.winfo_exists():
                        self.after(0, ui_err)
                except Exception as e:
                    pass

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def _finish_buttons(self):
        self.progress.set(1.0)
        self.pause_btn.configure(state="disabled")
        self.resume_btn.configure(state="disabled")
        self.retry_btn.configure(state="disabled")
        self.cancel_btn.configure(state="disabled")

    # ------------------- Button actions -------------------
    def pause(self):
        if self.status == "downloading":
            self.pause_event.set()
            self._set_message("⏸ Paused", "orange")

    def resume(self):
        if self.status == "downloading":
            self.pause_event.clear()
            self._set_message("⏳ Downloading...", "orange")

    def retry(self):
        if self._stall_after_id is not None:
            try: self.after_cancel(self._stall_after_id)
            except Exception: pass
            self._stall_after_id = None

        # error yoki cancelled holatdan qayta boshlash
        if self.status not in ("error", "cancelled"):
            return

        # 1) signal/event reset
        self.cancel_event.clear()
        self.pause_event.clear()

        # 2) progress throttle reset (aks holda UI yangilanmay qolishi mumkin)
        if self._prog_after_id is not None:
            try:
                self.after_cancel(self._prog_after_id)
            except Exception:
                pass
            self._prog_after_id = None
        self._pending_prog = None
        self._last_ui_prog_ts = 0.0

        # 3) UI reset
        self.status = "downloading"
        try:
            if self.winfo_exists() and not getattr(self.winfo_toplevel(), "is_closing", False):
                self.progress.set(0)
                self.info_label.configure(text="ETA: -- | Speed: --")
                self._set_message("🔁 Retrying...", "orange")
        except Exception:
            pass

        # 4) qayta start
        self.start_download()


    def cancel(self):
        if self._stall_after_id is not None:
            try: self.after_cancel(self._stall_after_id)
            except Exception: pass
            self._stall_after_id = None

        if self.status in ("downloading", "error", "cancelled"):
            # UI update'lar kelmasin
            self.cancel_event.set()
            self.status = "cancelled"

            # pending after'larni ham to'xta
            if self._prog_after_id is not None:
                try: self.after_cancel(self._prog_after_id)
                except Exception: pass
                self._prog_after_id = None
            self._pending_prog = None

            # msg_label bo'lsa ham keyin destroy bo'ladi, lekin set qilish xavfli:
            if self.winfo_exists() and not getattr(self.winfo_toplevel(), "is_closing", False):
                try:
                    self._set_message("✖ Cancelled", "red")
                    self.info_label.configure(text="ETA: -- | Speed: --")
                    self.progress.set(0)
                except Exception:
                    pass



# ==================== DOWNLOADER PAGE ====================
class DownloaderPage(ctk.CTkFrame):
    VIDEO_RESOLUTIONS = ["1080p", "720p", "480p", "360p", "240p", "144p"]
    AUDIO_RESOLUTIONS = ["320kbps", "192kbps", "128kbps"]
    SUBTITLES_LIST=[('Manual', ''), ('ab (auto)', 'ab:auto'), ('aa (auto)', 'aa:auto'), ('af (auto)', 'af:auto'), ('ak (auto)', 'ak:auto'), ('sq (auto)', 'sq:auto'), ('am (auto)', 'am:auto'), ('ar (auto)', 'ar:auto'), ('hy (auto)', 'hy:auto'), ('as (auto)', 'as:auto'), ('ay (auto)', 'ay:auto'), ('az (auto)', 'az:auto'), ('bn (auto)', 'bn:auto'), ('ba (auto)', 'ba:auto'), ('eu (auto)', 'eu:auto'), ('be (auto)', 'be:auto'), ('bho (auto)', 'bho:auto'), ('bs (auto)', 'bs:auto'), ('br (auto)', 'br:auto'), ('bg (auto)', 'bg:auto'), ('my (auto)', 'my:auto'), ('ca (auto)', 'ca:auto'), ('ceb (auto)', 'ceb:auto'), ('zh-Hans (auto)', 'zh-Hans:auto'), ('zh-Hant (auto)', 'zh-Hant:auto'), ('co (auto)', 'co:auto'), ('hr (auto)', 'hr:auto'), ('cs (auto)', 'cs:auto'), ('da (auto)', 'da:auto'), ('dv (auto)', 'dv:auto'), ('nl (auto)', 'nl:auto'), ('dz (auto)', 'dz:auto'), ('en-orig (auto)', 'en-orig:auto'), ('en (auto)', 'en:auto'), ('eo (auto)', 'eo:auto'), ('et (auto)', 'et:auto'), ('ee (auto)', 'ee:auto'), ('fo (auto)', 'fo:auto'), ('fj (auto)', 'fj:auto'), ('fil (auto)', 'fil:auto'), ('fi (auto)', 'fi:auto'), ('fr (auto)', 'fr:auto'), ('gaa (auto)', 'gaa:auto'), ('gl (auto)', 'gl:auto'), ('lg (auto)', 'lg:auto'), ('ka (auto)', 'ka:auto'), ('de (auto)', 'de:auto'), ('el (auto)', 'el:auto'), ('gn (auto)', 'gn:auto'), ('gu (auto)', 'gu:auto'), ('ht (auto)', 'ht:auto'), ('ha (auto)', 'ha:auto'), ('haw (auto)', 'haw:auto'), ('iw (auto)', 'iw:auto'), ('hi (auto)', 'hi:auto'), ('hmn (auto)', 'hmn:auto'), ('hu (auto)', 'hu:auto'), ('is (auto)', 'is:auto'), ('ig (auto)', 'ig:auto'), ('id (auto)', 'id:auto'), ('iu (auto)', 'iu:auto'), ('ga (auto)', 'ga:auto'), ('it (auto)', 'it:auto'), ('ja (auto)', 'ja:auto'), ('jv (auto)', 'jv:auto'), ('kl (auto)', 'kl:auto'), ('kn (auto)', 'kn:auto'), ('kk (auto)', 'kk:auto'), ('kha (auto)', 'kha:auto'), ('km (auto)', 'km:auto'), ('rw (auto)', 'rw:auto'), ('ko (auto)', 'ko:auto'), ('kri (auto)', 'kri:auto'), ('ku (auto)', 'ku:auto'), ('ky (auto)', 'ky:auto'), ('lo (auto)', 'lo:auto'), ('la (auto)', 'la:auto'), ('lv (auto)', 'lv:auto'), ('ln (auto)', 'ln:auto'), ('lt (auto)', 'lt:auto'), ('lua (auto)', 'lua:auto'), ('luo (auto)', 'luo:auto'), ('lb (auto)', 'lb:auto'), ('mk (auto)', 'mk:auto'), ('mg (auto)', 'mg:auto'), ('ms (auto)', 'ms:auto'), ('ml (auto)', 'ml:auto'), ('mt (auto)', 'mt:auto'), ('gv (auto)', 'gv:auto'), ('mi (auto)', 'mi:auto'), ('mr (auto)', 'mr:auto'), ('mn (auto)', 'mn:auto'), ('mfe (auto)', 'mfe:auto'), ('ne (auto)', 'ne:auto'), ('new (auto)', 'new:auto'), ('nso (auto)', 'nso:auto'), ('no (auto)', 'no:auto'), ('ny (auto)', 'ny:auto'), ('oc (auto)', 'oc:auto'), ('or (auto)', 'or:auto'), ('om (auto)', 'om:auto'), ('os (auto)', 'os:auto'), ('pam (auto)', 'pam:auto'), ('ps (auto)', 'ps:auto'), ('fa (auto)', 'fa:auto'), ('pl (auto)', 'pl:auto'), ('pt (auto)', 'pt:auto'), ('pt-PT (auto)', 'pt-PT:auto'), ('pa (auto)', 'pa:auto'), ('qu (auto)', 'qu:auto'), ('ro (auto)', 'ro:auto'), ('rn (auto)', 'rn:auto'), ('ru (auto)', 'ru:auto'), ('sm (auto)', 'sm:auto'), ('sg (auto)', 'sg:auto'), ('sa (auto)', 'sa:auto'), ('gd (auto)', 'gd:auto'), ('sr (auto)', 'sr:auto'), ('crs (auto)', 'crs:auto'), ('sn (auto)', 'sn:auto'), ('sd (auto)', 'sd:auto'), ('si (auto)', 'si:auto'), ('sk (auto)', 'sk:auto'), ('sl (auto)', 'sl:auto'), ('so (auto)', 'so:auto'), ('st (auto)', 'st:auto'), ('es (auto)', 'es:auto'), ('su (auto)', 'su:auto'), ('sw (auto)', 'sw:auto'), ('ss (auto)', 'ss:auto'), ('sv (auto)', 'sv:auto'), ('tg (auto)', 'tg:auto'), ('ta (auto)', 'ta:auto'), ('tt (auto)', 'tt:auto'), ('te (auto)', 'te:auto'), ('th (auto)', 'th:auto'), ('bo (auto)', 'bo:auto'), ('ti (auto)', 'ti:auto'), ('to (auto)', 'to:auto'), ('ts (auto)', 'ts:auto'), ('tn (auto)', 'tn:auto'), ('tum (auto)', 'tum:auto'), ('tr (auto)', 'tr:auto'), ('tk (auto)', 'tk:auto'), ('uk (auto)', 'uk:auto'), ('ur (auto)', 'ur:auto'), ('ug (auto)', 'ug:auto'), ('uz (auto)', 'uz:auto'), ('ve (auto)', 've:auto'), ('vi (auto)', 'vi:auto'), ('war (auto)', 'war:auto'), ('cy (auto)', 'cy:auto'), ('fy (auto)', 'fy:auto'), ('wo (auto)', 'wo:auto'), ('xh (auto)', 'xh:auto'), ('yi (auto)', 'yi:auto'), ('yo (auto)', 'yo:auto'), ('zu (auto)', 'zu:auto')]
    def __init__(self, parent, settings: SettingsManager, dashboard: DashboardPage, history_page):
        super().__init__(parent)
        self._formats_lock = threading.Lock()

        self.cards = []
        self.settings = settings
        self.dashboard = dashboard
        self.history_page = history_page
        self._clip_after_id = None
        self._last_clip = ""
        self._sub_map = {lbl.strip(): code for lbl, code in self.SUBTITLES_LIST}
        self._pending_cards=None


        # ---------------- Top Controls ----------------
        top_frame = ctk.CTkFrame(self)
        top_frame.pack(fill="x", pady=10, padx=20)

        ctk.CTkLabel(top_frame, text="Mode:").grid(row=0, column=0, padx=(0, 5))
        self.mode_var = ctk.StringVar(value="Single")
        ctk.CTkOptionMenu(
            top_frame, values=["Single", "Batch"], variable=self.mode_var
        ).grid(row=0, column=1, padx=(0, 15))

        ctk.CTkLabel(top_frame, text="Media Type:").grid(row=0, column=2, padx=(0, 5))
        self.media_var = ctk.StringVar(value="Video")
        ctk.CTkOptionMenu(
            top_frame, values=["Video", "Audio"],
            variable=self.media_var, command=self.media_changed
        ).grid(row=0, column=3, padx=(0, 15))

        ctk.CTkLabel(top_frame, text="Resolution:") \
            .grid(row=0, column=4, padx=(0, 5), sticky="w")

        self.res_var = ctk.StringVar(value="Manual")
        self.res_menu = ctk.CTkOptionMenu(
            top_frame, values=["Manual"],
            variable=self.res_var, command=self.res_changed
        )
        self.res_menu.grid(row=0,  column=5, sticky="w", padx=(0, 15))

        #adding subtitle selection
        self.sub_var = ctk.StringVar(value="None")
        self.sub_menu = ScrollableOptionMenu(
            top_frame,
            values=[label for (label, _code) in self.SUBTITLES_LIST],
            variable=self.sub_var,
            command=self.sub_changed,
            width=180,
            max_visible=10
        )
        self.sub_menu.grid(row=0, column=7, padx=(0, 15), sticky="w")
        ctk.CTkLabel(top_frame, text="Subtitle:").grid(row=0, column=6, padx=(0, 5), sticky="w")



        self.reveal_btn = ctk.CTkButton(
            top_frame, text="Reveal Folder", width=130,
            command=self._reveal_default_folder
        )
        self.reveal_btn.grid(row=0, column=8, padx=(0, 15), sticky="w")

        self.clipboard_var = ctk.BooleanVar(value=False)
        self.clip_switch = ctk.CTkSwitch(
            top_frame, text="Copy monitor",
            variable=self.clipboard_var,
            command=self._toggle_clipboard_monitor
        )
        self.clip_switch.grid(row=0, column=9, padx=(0, 0), sticky="w")

        # entry for links
        ctk.CTkLabel(self, text="Enter link(s):").pack(anchor="w", padx=20)
        self.link_entry = ctk.CTkTextbox(self, height=80)
        self.link_entry.pack(fill="x", padx=20, pady=(0, 10))


        #fetch frame
        self.res_frame = ctk.CTkFrame(self)
        self.res_frame.pack(fill="x", padx=20, pady=(0, 10))

        # column config
        self.res_frame.grid_columnconfigure(0, weight=0)
        self.res_frame.grid_columnconfigure(1, weight=1)# ← spacer

        self.action_button = ctk.CTkButton(
            self.res_frame,
            text="Fetch Data",
            command=self.action_clicked
        )
        self.action_button.grid(row=0, column=1, sticky="e", padx=(0, 5))


        # ---------------- Scrollable Cards (CTkScrollableFrame) ----------------
        self.cards_container = ctk.CTkScrollableFrame(self)
        self.cards_container.pack(fill="both", expand=True, padx=20, pady=(0, 20))

        # FAST wheel: page-scrolling (history dagidek)
        self._install_wheel(self.cards_container)

        self._formats_cache = {}  # url -> formats dict
        self.media_changed(self.media_var.get())

        # ----------------- callbacks -----------------

    def media_changed(self, media_type: str):
        if media_type == "Video":
            # resolution menu
            self.res_menu.configure(values=["Manual"] + self.VIDEO_RESOLUTIONS)
            self.res_var.set("Manual")

            self.sub_var.set("Manual")
            self.sub_menu.configure(values=["Manual"], state="disabled")
            # subtitle menu: restore FULL list
            if self.res_var.get() != "Manual":
                self.sub_menu.configure(
                    values=[label for (label, _code) in self.SUBTITLES_LIST],
                    state="normal"
                )
                self.sub_var.set("Manual")

        else:  # Audio
            self.res_menu.configure(values=["Manual"] + self.AUDIO_RESOLUTIONS)
            self.res_var.set("Manual")

            # subtitle menu: clamp to None
            self.sub_var.set("Manual")
            self.sub_menu.configure(values=["Manual"], state="disabled")

        self.update_action_button()


    def res_changed(self, _value=None):
        if self.media_var.get() == "Video" and self.res_var.get() != "Manual":
            self.sub_menu.configure(
                values=[label for (label, _code) in self.SUBTITLES_LIST],
                state="normal"
            )
            self.sub_var.set("Manual")
        else:
            self.sub_var.set("Manual")
            self.sub_menu.configure(state="disabled")
        self.update_action_button()


    def sub_changed(self, _value=None):
        self.update_action_button()


    # ---------- Wheel (History bilan bir xil tez rejim) ----------
    def _install_wheel(self, scrollable: ctk.CTkScrollableFrame):
        canvas = getattr(scrollable, "_parent_canvas", None)
        if canvas is None:
            return

        def on_wheel(e):
            canvas = getattr(scrollable, "_parent_canvas", None)
            if canvas is None:
                return "break"

            # Windows/macOS
            if getattr(e, "delta", 0):
                delta = e.delta

                # Wheel notch
                if abs(delta) >= 120:
                    steps = int(-delta / 120)
                    canvas.yview_scroll(steps * 1, "pages")  # tez
                    return "break"

                # Touchpad (kichik delta) ham tez bo'lsin
                steps = -1 if delta > 0 else 1
                canvas.yview_scroll(steps * 1, "pages")
                return "break"

            # Linux
            if getattr(e, "num", None) == 5:
                canvas.yview_scroll(1, "pages")
                return "break"
            if getattr(e, "num", None) == 4:
                canvas.yview_scroll(-1, "pages")
                return "break"

            return "break"

        canvas.bind("<MouseWheel>", on_wheel)
        canvas.bind("<Button-4>", on_wheel)
        canvas.bind("<Button-5>", on_wheel)

        scrollable.bind("<MouseWheel>", on_wheel)
        scrollable.bind("<Button-4>", on_wheel)
        scrollable.bind("<Button-5>", on_wheel)

        self._cards_wheel_handler = on_wheel

    def _bind_wheel_recursive(self, widget):
        h = getattr(self, "_cards_wheel_handler", None)
        if not h:
            return
        widget.bind("<MouseWheel>", h)
        widget.bind("<Button-4>", h)
        widget.bind("<Button-5>", h)
        for ch in widget.winfo_children():
            self._bind_wheel_recursive(ch)

    # ---------- Existing logic ----------
    def stop_all(self):
        self._stop_clipboard_monitor()
        for w in self.cards_container.winfo_children():
            # w ning 'stop' metodi borligini va u chaqiriladigan (callable) ekanini tekshiramiz
            stop_func = getattr(w, "stop", None)
            if callable(stop_func):
                try:
                    stop_func()
                except Exception:
                    pass


    def _reveal_default_folder(self):
        folder = self.settings.data.get("download_dir", "Downloads")
        folder = os.path.abspath(folder)
        try:
            subprocess.run(["explorer", folder], check=False)
        except Exception:
            pass

    def _toggle_clipboard_monitor(self):
        if self.clipboard_var.get():
            self._start_clipboard_monitor()
        else:
            self._stop_clipboard_monitor()

    def _start_clipboard_monitor(self):
        if self._clip_after_id:
            return
        self._poll_clipboard()

    def _stop_clipboard_monitor(self):
        if self._clip_after_id:
            try:
                self.after_cancel(self._clip_after_id)
            except Exception:
                pass
            self._clip_after_id = None

    def _poll_clipboard(self):
        root = self.winfo_toplevel()
        if not self.winfo_exists() or not self.clipboard_var.get():
            self._clip_after_id = None
            return
        if getattr(root, "is_closing", False):
            self._clip_after_id = None
            return

        try:
            text = self.clipboard_get()
        except Exception:
            text = ""

        text = (text or "").strip()

        if text and text != self._last_clip and self._looks_like_url(text):
            self._last_clip = text
            self._append_link(text)
        else:
            if text:
                self._last_clip = text

        self._clip_after_id = self.after(600, self._poll_clipboard)

    def _looks_like_url(self, s: str) -> bool:
        s = s.lower().strip()
        return s.startswith("http://") or s.startswith("https://")

    def _append_link(self, url: str):
        current = self.link_entry.get("1.0", "end").strip()
        lines = [l.strip() for l in current.splitlines() if l.strip()]
        if url in lines:
            return

        if self.mode_var.get() == "Single":
            self.link_entry.delete("1.0", "end")
            self.link_entry.insert("1.0", url)
        else:
            if current:
                self.link_entry.insert("end", "\n" + url)
            else:
                self.link_entry.insert("1.0", url)



    def update_action_button(self):
        self.action_button.configure(text="Fetch Data" if self.res_var.get() == "Manual" else "Download")

    def _get_links(self):
        if self.mode_var.get() == "Batch":
            links = self.link_entry.get("1.0", "end").strip().split("\n")
        else:
            links = [self.link_entry.get("1.0", "end").strip().split("\n")[0]]
        return [l.strip() for l in links if l.strip()]

    def _action_clicked_continue(self):
        links = self._get_links()
        if not links:
            return

        if self.res_var.get() == "Manual":
            self.action_button.configure(state="disabled", text="Fetching...")
            threading.Thread(target=self._fetch_formats_thread, args=(links,), daemon=True).start()
            return

        if self.res_var.get() != "Manual":
            chosen_quality = self.res_var.get()
            label = (self.sub_var.get() or "").strip()
            chosen_sub = self._sub_map.get(label, "")
            
            for url in links:
                card = DownloadCard(
                    self.cards_container,
                    title="Loading...",
                    link=url,
                    media_type=self.media_var.get(),
                    quality=chosen_quality,
                    settings=self.settings,
                    dashboard=self.dashboard,
                    history_page=self.history_page,
                    thumbnail_url="",   # hozircha yo‘q
                    subtitle_lang=chosen_sub,   # <-- NEW
                )
                card.pack(fill="x", expand=True, pady=5)
                self._bind_wheel_recursive(card)
                self.cards.append(card)

                threading.Thread(target=self._fill_card_meta, args=(card, url), daemon=True).start()
            return


    def action_clicked(self):
        # 1) cancel all running cards
        for c in list(self.cards):
            if hasattr(c, "cancel"):
                try: c.cancel()
                except Exception: pass

        # 2) destroy biroz kechikib (race kamayadi)
        def _clear_ui():
            for w in self.cards_container.winfo_children():
                try: w.destroy()
                except Exception: pass
            self.cards.clear()
            self._action_clicked_continue()   # action logikani shu funksiya ichiga ko'chir

        self.after(120, _clear_ui)

    
    def _fill_card_meta(self, card: "DownloadCard", url: str):
        try:
            fmts = extract_formats(url)  # download=False metadata
            title = (fmts.get("title") or "").strip()
            thumb = (fmts.get("thumbnail") or "").strip()

            def ui():
                if (not card.winfo_exists()) or getattr(card.winfo_toplevel(), "is_closing", False):
                    return
                card.update_meta(title=title, thumbnail_url=thumb)

            self.after(0, ui)
        except Exception:
            pass


    def _fetch_formats_thread(self, links):
        try:
            for url in links:
                with self._formats_lock:
                    fmts = self._formats_cache.get(url)

                if fmts is None:
                    fmts = extract_formats(url)
                    with self._formats_lock:
                        self._formats_cache[url] = fmts

                upsert_history(
                    url=url,
                    type_=self.media_var.get(),
                    title=fmts.get("title", "Fetched"),
                    fetched_formats=(
                        fmts.get("video_resolutions")
                        if self.media_var.get() == "Video"
                        else fmts.get("audio_bitrates")
                    ),
                    last_status="fetched"
                )

            def ui_done():
                self.res_var.set("Manual")
                self.action_button.configure(state="normal", text="Fetch Data")

                for w in self.cards_container.winfo_children():
                    w.destroy()
                self.cards.clear()

                media = self.media_var.get()
                self._pending_cards = list(links)

                def _add_next_chunk():
                    if not self._pending_cards:
                        return

                    for _ in range(4):
                        if not self._pending_cards:
                            break

                        url = self._pending_cards.pop(0)
                        with self._formats_lock:
                            fmts = self._formats_cache.get(url, {})
                        
                        

                        title = fmts.get("title", "Unknown")
                        qualities = (
                            fmts.get("video_resolutions") if media == "Video"
                            else fmts.get("audio_bitrates")
                        )

                        subs = fmts.get("subtitles") or [("None", "")]
                        thumb = fmts.get("thumbnail") or ""
                        card = ManualFormatCard(
                            self.cards_container,
                            url=url,
                            title=title,
                            media_type=media,
                            qualities=qualities,
                            start_cb=self._start_manual_card_download,
                            thumbnail_url=thumb,
                            subtitles=subs,   # <-- NEW
                        )
                        card.pack(fill="x", expand=True, pady=5)
                        self._bind_wheel_recursive(card)
                        self.cards.append(card)

                    if not self.winfo_exists() or getattr(self.winfo_toplevel(), "is_closing", False):
                        return

                    self.after(1, _add_next_chunk)

                _add_next_chunk()

            self.after(0, ui_done)

        except Exception as e:
            self.after(0, lambda err=e: self._show_fetch_error(err))

    def _start_manual_card_download(self, manual_card: ManualFormatCard, url: str, quality: str, subtitle_lang: str):
        pack_info = manual_card.pack_info()
        manual_card.destroy()

        dl = DownloadCard(
            self.cards_container,
            title=f"{self.media_var.get()} File",
            link=url,
            thumbnail_url=manual_card.thumbnail_url,
            media_type=self.media_var.get(),
            quality=quality,
            settings=self.settings,
            dashboard=self.dashboard,
            history_page=self.history_page,
            subtitle_lang=subtitle_lang or "",   # <-- NEW
        )
        dl.pack(**pack_info)
        self._bind_wheel_recursive(dl)
        self.cards.append(dl)

    def start_download_from_history(self, url: str, media_type: str, quality: str):
        self.media_var.set(media_type)
        self.media_changed(media_type)
        self.res_var.set(quality)
        self.update_action_button()

        for widget in self.cards_container.winfo_children():
            widget.destroy()
        self.cards.clear()

        card = DownloadCard(
            self.cards_container,
            title=f"{media_type} File",
            link=url,
            media_type=media_type,
            quality=quality,
            settings=self.settings,
            dashboard=self.dashboard,
            history_page=self.history_page
        )
        card.pack(fill="x", expand=True, pady=5)
        self._bind_wheel_recursive(card)
        self.cards.append(card)

    def _show_fetch_error(self, e):
        self.action_button.configure(state="normal", text="Fetch Data")

        for w in self.cards_container.winfo_children():
            w.destroy()
        self.cards.clear()

        err_card = ctk.CTkFrame(self.cards_container, corner_radius=10, fg_color="#222")
        err_card.pack(fill="x", expand=True, pady=5)

        ctk.CTkLabel(
            err_card, text="Fetch Error", text_color="red",
            font=("Arial", 14, "bold")
        ).pack(anchor="w", padx=10, pady=(8, 0))

        ctk.CTkLabel(
            err_card, text=str(e), text_color="red",
            wraplength=900
        ).pack(anchor="w", padx=10, pady=(0, 10))




class ScrollableOptionMenu(ctk.CTkFrame):
    """
    Lightweight scrollable dropdown:
    - Click -> opens a small Toplevel with Listbox + Scrollbar
    - No search, just scroll
    - Calls command(label) on pick
    """
    def __init__(self, parent, values, variable, command=None, width=180, height=32, max_visible=16):
        super().__init__(parent, fg_color="transparent")
        self._values = list(values or [])
        self._var = variable
        self._cmd = command
        self._popup = None
        self._max_visible = max_visible

        self.btn = ctk.CTkButton(
            self,
            text=self._var.get() or (self._values[0] if self._values else ""),
            width=width,
            height=height,
            anchor="w",
            command=self._toggle
        )
        self.btn.pack(fill="x")

        # var sync
        try:
            self._var.trace_add("write", lambda *_: self.btn.configure(text=self._var.get() or ""))
        except Exception:
            pass

    def configure(self, **kwargs):
        if "values" in kwargs:
            self._values = list(kwargs.pop("values") or [])
        if "state" in kwargs:
            st = kwargs.pop("state")
            self.btn.configure(state=st)
        if kwargs:
            self.btn.configure(**kwargs)

    def _toggle(self):
        if self._popup and self._popup.winfo_exists():
            self._close()
        else:
            self._open()

    def _open(self):
        if not self._values:
            return

        self._popup = tk.Toplevel(self)
        self._popup.overrideredirect(True)
        self._popup.attributes("-topmost", True)
        self._popup.configure(bg="#1f1f1f")  # dark outer bg

        bx = self.btn.winfo_rootx()
        by = self.btn.winfo_rooty()
        bw = self.btn.winfo_width()
        bh = self.btn.winfo_height()

        row_h = 28  # kattaroq row
        visible = min(self._max_visible, len(self._values))
        ph = visible * row_h + 8

        self._popup.geometry(f"{bw}x{ph}+{bx}+{by+bh+4}")

        container = tk.Frame(self._popup, bg="#2b2b2b", bd=0)
        container.pack(fill="both", expand=True)

        lb = tk.Listbox(
            container,
            bg="#2b2b2b",
            fg="white",
            selectbackground="#1f6aa5",
            selectforeground="white",
            activestyle="none",
            highlightthickness=0,
            bd=0,
            relief="flat",
            font=("Segoe UI", 12),   # 🔥 kattaroq font
            height=visible
        )

        sb = tk.Scrollbar(
            container,
            orient="vertical",
            command=lb.yview,
            troughcolor="#2b2b2b",
            bg="#3a3a3a",
            activebackground="#4a4a4a",
            bd=0
        )

        lb.configure(yscrollcommand=sb.set)

        lb.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=6)
        sb.pack(side="right", fill="y", padx=(0, 6), pady=6)

        cur = self._var.get()
        for i, v in enumerate(self._values):
            lb.insert("end", v)
            if v == cur:
                lb.selection_set(i)
                lb.see(i)

        def pick(_evt=None):
            sel = lb.curselection()
            if not sel:
                return
            label = self._values[int(sel[0])]
            self._var.set(label)
            if self._cmd:
                self._cmd(label)
            self._close()

        lb.bind("<Double-Button-1>", pick)
        lb.bind("<Return>", pick)

        def on_wheel(e):
            if getattr(e, "delta", 0):
                lb.yview_scroll(int(-e.delta / 120), "units")
                return "break"
            if getattr(e, "num", None) == 5:
                lb.yview_scroll(1, "units"); return "break"
            if getattr(e, "num", None) == 4:
                lb.yview_scroll(-1, "units"); return "break"
            return "break"

        lb.bind("<MouseWheel>", on_wheel)
        lb.bind("<Button-4>", on_wheel)
        lb.bind("<Button-5>", on_wheel)

        self._popup.bind("<FocusOut>", lambda _e: self._close())
        self._popup.focus_force()
        lb.focus_set()


    def _close(self):
        if self._popup and self._popup.winfo_exists():
            try:
                self._popup.destroy()
            except Exception:
                pass
        self._popup = None



# ---- History Page ----
class HistoryPage(ctk.CTkFrame):
    def __init__(self, parent, on_download_click=None):
        super().__init__(parent)
        self.on_download_click = on_download_click  # callback(url, type, quality)

        ctk.CTkLabel(self, text="History", font=("Arial", 24)).pack(pady=20)

        # --- Scrollable list (NO custom canvas) ---
        self.list_frame = ctk.CTkScrollableFrame(self)
        self.list_frame.pack(fill="both", expand=True, padx=20, pady=10)

        # wheel (child ustida ham ishlashi uchun)
        self._install_wheel(self.list_frame)

        self.refresh()

    def _install_wheel(self, scrollable: ctk.CTkScrollableFrame):
        canvas = getattr(scrollable, "_parent_canvas", None)
        if canvas is None:
            return

        def on_wheel(e):
            canvas = getattr(scrollable, "_parent_canvas", None)
            if canvas is None:
                return "break"

            # Windows/macOS
            if getattr(e, "delta", 0):
                delta = e.delta

                # Wheel notch (~120) => page scroll
                if abs(delta) >= 120:
                    steps = int(-delta / 120)
                    # juda tez: har notch 1 page (xohlasang 2 yoki 3 qil)
                    canvas.yview_scroll(steps * 1, "pages")
                    return "break"

                # Touchpad (kichik delta) => ham page scroll (tez)
                steps = -1 if delta > 0 else 1
                canvas.yview_scroll(steps * 1, "pages")
                return "break"

            # Linux
            if getattr(e, "num", None) == 5:
                canvas.yview_scroll(1, "pages")
                return "break"
            if getattr(e, "num", None) == 4:
                canvas.yview_scroll(-1, "pages")
                return "break"

            return "break"

        # bind only to this scrollable + its canvas (global emas)
        canvas.bind("<MouseWheel>", on_wheel)
        canvas.bind("<Button-4>", on_wheel)
        canvas.bind("<Button-5>", on_wheel)

        scrollable.bind("<MouseWheel>", on_wheel)
        scrollable.bind("<Button-4>", on_wheel)
        scrollable.bind("<Button-5>", on_wheel)

        self._wheel_handler = on_wheel  # ref saqlab tur

    def refresh(self):
        for w in self.list_frame.winfo_children():
            w.destroy()

        hist = load_history()[::-1]  # newest first
        if not hist:
            ctk.CTkLabel(
                self.list_frame,
                text="No history yet.",
                text_color="gray"
            ).pack(anchor="w", padx=10, pady=10)
            return

        for item in hist[:200]:
            self._render_item(item)

    def _render_item(self, item: dict):
        row = ctk.CTkFrame(self.list_frame)
        row.pack(fill="x", padx=5, pady=6)

        top = ctk.CTkFrame(row, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(8, 0))

        title = item.get("title", "Item")
        url = item.get("url", "")
        status = item.get("last_status", item.get("status", ""))
        dt = item.get("last_date", item.get("date", ""))
        q = item.get("quality", "")
        t = item.get("type", "")
        item_id = item.get("id", "")

        ctk.CTkLabel(top, text=f"{title}", font=("Arial", 13, "bold")).pack(side="left", anchor="w")

        ctk.CTkButton(
            top,
            text="Delete",
            width=70,
            fg_color="red",
            hover_color="#aa0000",
            command=lambda _id=item_id, u=url, typ=t: self._delete(_id, u, typ)
        ).pack(side="right")

        ctk.CTkLabel(row, text=f"{t} {q}  -  {status}  -  {dt}", text_color="gray").pack(anchor="w", padx=10)
        ctk.CTkLabel(row, text=url, text_color="gray", wraplength=950).pack(anchor="w", padx=10, pady=(0, 6))

        fetched = item.get("fetched_formats")
        if fetched and isinstance(fetched, list):
            btn_area = ctk.CTkFrame(row, fg_color="transparent")
            btn_area.pack(fill="x", padx=10, pady=(0, 8))

            max_cols = 9
            r = 0
            c = 0
            for fmt in fetched[:30]:
                if isinstance(fmt, (list, tuple)) and len(fmt) == 2:
                    display, value = fmt
                else:
                    display, value = str(fmt), str(fmt)

                b = ctk.CTkButton(
                    btn_area,
                    text=display,
                    width=120,
                    command=lambda u=url, typ=t, val=value: self._start_from_history(u, typ, val)
                )
                b.grid(row=r, column=c, padx=4, pady=3, sticky="w")

                c += 1
                if c >= max_cols:
                    c = 0
                    r += 1

        # child ustida wheel ishlashi uchun: row va ichidagilarga ham bind
        handler = getattr(self, "_wheel_handler", None)
        if handler:
            self._bind_wheel_recursive(row, handler)

    def _bind_wheel_recursive(self, widget, handler):
        widget.bind("<MouseWheel>", handler)
        widget.bind("<Button-4>", handler)
        widget.bind("<Button-5>", handler)
        for ch in widget.winfo_children():
            self._bind_wheel_recursive(ch, handler)

    def _delete(self, item_id: str, url: str, type_: str):
        delete_history_item(item_id=item_id, url=url, type_=type_)
        self.refresh()

    def _start_from_history(self, url: str, media_type: str, quality: str):
        if self.on_download_click:
            self.on_download_click(url, media_type, quality)
 



# ---- Settings Page (scrollable + fast wheel like History) ----
class SettingsPage(ctk.CTkFrame):
    def __init__(self, parent, settings: SettingsManager):
        super().__init__(parent)
        self.settings = settings

        ctk.CTkLabel(self, text="Settings", font=("Arial", 24)).pack(pady=(20, 10))

        # --- Scrollable content (History kabi) ---
        self.scroll = ctk.CTkScrollableFrame(self)
        self.scroll.pack(fill="both", expand=True, padx=20, pady=(0, 20))

        self._install_wheel(self.scroll)

        # Content wrapper (paddingni bir joyda ushlash)
        body = ctk.CTkFrame(self.scroll, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=10, pady=10)

        # ---------- Theme ----------
        ctk.CTkLabel(body, text="Theme").pack(anchor="w", padx=10)
        self.theme = ctk.CTkOptionMenu(body, values=["dark", "light", "system"], command=self.change_theme)
        self.theme.set(self.settings.data.get("theme", "dark"))
        self.theme.pack(fill="x", padx=10, pady=(0, 12))

        # ---------- Accent ----------
        ctk.CTkLabel(body, text="Accent Color").pack(anchor="w", padx=10)
        self.accent = ctk.CTkOptionMenu(body, values=["blue", "green", "dark-blue"], command=self.change_accent)
        self.accent.set(self.settings.data.get("accent", "blue"))
        self.accent.pack(fill="x", padx=10, pady=(0, 12))

        # ---------- Text size ----------
        ctk.CTkLabel(body, text="Text Size").pack(anchor="w", padx=10)
        self.text_size = ctk.CTkSlider(body, from_=1, to=2, number_of_steps=5, command=self.change_text)
        self.text_size.set(self.settings.data.get("text_scale", 1.0))
        self.text_size.pack(fill="x", padx=10, pady=(0, 12))

        # ---------- Download folder ----------
        ctk.CTkLabel(body, text="Download Folder").pack(anchor="w", padx=10)
        self.path_label = ctk.CTkLabel(
            body,
            text=os.path.abspath(self.settings.data.get("download_dir", "Downloads")),
            text_color="gray",
            wraplength=900
        )
        self.path_label.pack(anchor="w", padx=10, pady=(0, 8))
        ctk.CTkButton(body, text="Change Folder", command=self.select_download_folder).pack(
            anchor="w", padx=10, pady=(0, 12)
        )

        # ---------- Cookies ----------
        ctk.CTkLabel(body, text="Cookies File (optional)").pack(anchor="w", padx=10)
        self.cookies_label = ctk.CTkLabel(
            body,
            text=self.settings.data.get("cookie_file", "") or "Not set",
            text_color="gray",
            wraplength=900
        )
        self.cookies_label.pack(anchor="w", padx=10, pady=(0, 8))

        btn_row = ctk.CTkFrame(body, fg_color="transparent")
        btn_row.pack(fill="x", padx=10, pady=(0, 18))
        ctk.CTkButton(btn_row, text="Select cookies.txt", command=self.select_cookies_file).pack(
            side="left", padx=(0, 8)
        )
        ctk.CTkButton(btn_row, text="Clear cookies", fg_color="gray", command=self.clear_cookies).pack(
            side="left"
        )

        # ---------- Reset ----------
        ctk.CTkButton(
            body,
            text="Reset to Defaults",
            fg_color="red",
            hover_color="#aa0000",
            command=self.reset_defaults
        ).pack(anchor="w", padx=10, pady=(0, 10))

    # -------- wheel (History'dagi tez variant) --------
    def _install_wheel(self, scrollable: ctk.CTkScrollableFrame):
        canvas = getattr(scrollable, "_parent_canvas", None)
        if canvas is None:
            return

        def on_wheel(e):
            # Windows / macOS: e.delta
            if getattr(e, "delta", 0):
                delta = e.delta
                if abs(delta) >= 120:
                    steps = int(-delta / 120)
                else:
                    steps = -1 if delta > 0 else 1

                speed = 8  # tezroq (3..12)
                canvas.yview_scroll(steps * speed, "units")
                return "break"

            # Linux
            if getattr(e, "num", None) == 5:
                canvas.yview_scroll(8, "units")
                return "break"
            if getattr(e, "num", None) == 4:
                canvas.yview_scroll(-8, "units")
                return "break"

        canvas.bind("<MouseWheel>", on_wheel)
        canvas.bind("<Button-4>", on_wheel)
        canvas.bind("<Button-5>", on_wheel)

        scrollable.bind("<MouseWheel>", on_wheel)
        scrollable.bind("<Button-4>", on_wheel)
        scrollable.bind("<Button-5>", on_wheel)

        self._wheel_handler = on_wheel  # ref saqlab tur

    # -------- actions --------
    def select_cookies_file(self):
        path = filedialog.askopenfilename(
            parent=self.master,
            title="Select cookies.txt",
            filetypes=[("Cookies txt", "*.txt"), ("All files", "*.*")]
        )
        if not path:
            return
        self.settings.save({"cookie_file": path})
        self.cookies_label.configure(text=path)

    def clear_cookies(self):
        self.settings.save({"cookie_file": ""})
        self.cookies_label.configure(text="Not set")

    def change_theme(self, v):
        ctk.set_appearance_mode(v)
        self.settings.save({"theme": v})

    def change_accent(self, v):
        ctk.set_default_color_theme(v)
        self.settings.save({"accent": v})

    def change_text(self, v):
        v = round(float(v), 2)
        ctk.set_widget_scaling(v)
        self.settings.save({"text_scale": v})

    def reset_defaults(self):
        self.settings.reset()
        self.theme.set(self.settings.data.get("theme", "dark"))
        self.accent.set(self.settings.data.get("accent", "blue"))
        self.text_size.set(self.settings.data.get("text_scale", 1.0))
        self.path_label.configure(text=os.path.abspath(self.settings.data.get("download_dir", "Downloads")))
        self.cookies_label.configure(text=self.settings.data.get("cookie_file", "") or "Not set")

    def select_download_folder(self):
        folder = filedialog.askdirectory(parent=self.master, title="Select Download Folder")
        if not folder:
            folder = self.settings.DEFAULTS["download_dir"]
        self.settings.save({"download_dir": folder})
        os.makedirs(folder, exist_ok=True)
        self.path_label.configure(text=os.path.abspath(folder))



# ==================== CENTER CONTENT ====================
class CenterContent(ctk.CTkFrame):
    def __init__(self, parent, settings):
        super().__init__(parent, corner_radius=0)
        self.grid(row=1, column=0, sticky="nsew")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.pages = {}
        
        self.pages["dashboard"] = DashboardPage(self, settings)

        # create downloader first as None placeholder
        self.pages["downloader"] = None

        # History gets callback that starts download in downloader and also navigates to downloader page
        def _history_download_click(url, media_type, quality):
            self.show_page("downloader")
            self.pages["downloader"].start_download_from_history(url, media_type, quality)

        self.pages["history"] = HistoryPage(self, on_download_click=_history_download_click)
        self.pages["settings"] = SettingsPage(self, settings)

        # now create downloader with history reference
        self.pages["downloader"] = DownloaderPage(self, settings, self.pages["dashboard"], self.pages["history"])


        
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")

        self.show_page("downloader")

    def show_page(self, name):
        self.pages[name].tkraise()
    
    def stop_all(self):
        # Stop downloader cards’ background threads and after loops
        dl = self.pages.get("downloader")
        if dl and hasattr(dl, "stop_all"):
            dl.stop_all()

        # Stop any other repeating after loops if you have them
        for p in self.pages.values():
            if hasattr(p, "stop_all") and p is not dl:
                p.stop_all()



# ==================== CENTER CONTAINER ====================
class CenterContainer(ctk.CTkFrame):
    def __init__(self, parent, toggle_callback, settings):
        super().__init__(parent, corner_radius=0)
        # self.grid(row=0, column=1, sticky="nsew")
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.topbar = CenterTopBar(self, toggle_callback)
        self.content = CenterContent(self, settings)


# ==================== MAIN APP ====================
class App(ctk.CTk):
    BP = 820
    def __init__(self):
        super().__init__()

        def _safe_report_callback_exception(exc, val, tb):
            # app yopilayotganida tk callback xatolarini yutib yubor
            if getattr(self, "is_closing", False):
                return
            import traceback
            traceback.print_exception(exc, val, tb)

        self.report_callback_exception = _safe_report_callback_exception


        self._bgutil_proc = None
        self._bgutil_server_js = os.path.join(
            os.path.expanduser("~"),
            "bgutil-ytdlp-pot-provider",
            "server",
            "build",
            "main.js",
        )

        # UI chiqqandan keyin bgutil'ni orqada ko'taramiz
        self.after(1, self._start_bgutil_async)



        self.is_closing = False

        self.settings = SettingsManager()
        
        threading.Thread(target=dedupe_history, daemon=True).start()


        self.title("Media Downloader")
        self.geometry("1100x650")

        self.sidebar_width = 220
        self.sidebar_visible = True

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)


        self.center = CenterContainer(self, self.toggle_drawer, self.settings)

        self.center.grid(row=0, column=0, sticky="nsew")  # GRID FAQAT SHU YERDA

        self._ui_init_drawer()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _cancel_all_afters(self):
        # Tcl interpreter ichidagi hamma pending after'larni bekor qiladi
        try:
            ids = self.tk.call("after", "info")
        except Exception:
            return

        for aid in ids:
            try:
                self.after_cancel(aid)
            except Exception:
                pass


    def _on_close(self):
        if getattr(self, "is_closing", False):
            return
        self.is_closing = True
     
        try:
            self.withdraw()
        except Exception:
            pass

        # 1) sening page/card after looplari
        try:
            if hasattr(self.center, "content"):
                self.center.content.stop_all()
        except Exception:
            pass

        # 2) bgutil to‘xtat
        try:
            stop_bgutil(self._bgutil_proc)
        except Exception:
            pass

        # 3) CustomTkinter/Tk pending after'lar — MUHIM
        self._cancel_all_afters()

        # 4) mainloop’dan chiq
        try:
            self.quit()
        except Exception:
            pass


    
    def _start_bgutil_async(self):
        def worker():
            try:
                proc = start_bgutil_if_needed(self._bgutil_server_js)
                self._bgutil_proc = proc
            except Exception as e:
                if self.winfo_exists() and not getattr(self, "is_closing", False):
                    try:
                        self.after(0, lambda: print("BGUTIL start failed:", e))
                    except Exception:
                        pass
        threading.Thread(target=worker, daemon=True).start()


    def _ui_init_drawer(self):
        self._drawer_open = False

        self._backdrop = ctk.CTkFrame(self, fg_color=("gray10", "gray10"), corner_radius=0)
        self._backdrop.place_forget()
        self._backdrop.bind("<Button-1>", lambda e: self.hide_drawer())

        # width/height ni konstruktorda BERMA — drawer width’ni show_drawer’da configure qilamiz
        self._drawer = ctk.CTkFrame(self, corner_radius=0)
        self._drawer.place_forget()

        self._drawer_sidebar = Sidebar(self._drawer, self.show_page)
        self._drawer_sidebar.pack(fill="both", expand=True)

        self.bind("<Escape>", lambda e: self.hide_drawer())


    def show_drawer(self):
        if self._drawer_open:
            return
        self._drawer_open = True

        self.update_idletasks()
        w = self.winfo_width()

        drawer_w = min(340, int(w * 0.78))

        # backdrop full screen (NO width/height)
        self._backdrop.place(x=0, y=0, relwidth=1, relheight=1)

        # drawer: set width via configure, then place
        self._drawer.configure(width=drawer_w)
        self._drawer.place(x=0, y=0, relheight=1)

        self._backdrop.lift()
        self._drawer.lift()

 

    def hide_drawer(self):
        if not self._drawer_open:
            return
        self._drawer_open = False
        self._drawer.place_forget()
        self._backdrop.place_forget()

    def toggle_drawer(self):
        if self._drawer_open:
            self.hide_drawer()
        else:
            self.show_drawer()


    def show_page(self, page):
        self.center.content.show_page(page)

        # drawer sidebar active highlight
        if hasattr(self, "_drawer_sidebar"):
            self._drawer_sidebar.set_active(page)

        # auto-close after navigation
        self.hide_drawer()


