"""
YTScrape – modern desktop GUI for downloading YouTube media, chaining clips
into one file with auto-generated chapters, and splitting long videos into
one file per timestamp/chapter.

Built on yt-dlp (https://github.com/yt-dlp/yt-dlp) + ffmpeg.
"""

from __future__ import annotations

import io
import json
import os
import queue
import subprocess
import sys
import threading
import traceback
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import customtkinter as ctk
from PIL import Image, ImageDraw
from tkinter import filedialog, messagebox

import yt_dlp
import core
from core import (APP_NAME, AUDIO_BITRATES, AUDIO_FORMATS, VIDEO_CONTAINERS, VIDEO_QUALITIES,
                  Cancelled, LinkItem, human_bytes, human_count, human_duration)

SETTINGS_FILE = core.APP_DIR / "settings.json"
THUMB_W, THUMB_H = 160, 90

ACCENT = ("#E5484D", "#E5484D")
ACCENT_HOVER = ("#C93A3F", "#F06A6E")
CARD_BG = ("#FFFFFF", "#232428")
CARD_SEL = ("#FFF1F1", "#2E2427")
APP_BG = ("#F2F3F5", "#18191C")
PANEL_BG = ("#FFFFFF", "#1F2023")
MUTED = ("#6B6F76", "#9A9DA3")
BORDER = ("#E3E5E8", "#2F3136")


def placeholder_thumb(text="…") -> Image.Image:
    img = Image.new("RGB", (THUMB_W * 2, THUMB_H * 2), "#2B2D31")
    d = ImageDraw.Draw(img)
    d.text((THUMB_W - 6, THUMB_H - 8), text, fill="#8E9297")
    return img


# --------------------------------------------------------------------------- #
# Card for one link
# --------------------------------------------------------------------------- #
class LinkCard(ctk.CTkFrame):
    def __init__(self, master, app: "App", item: LinkItem):
        super().__init__(master, fg_color=CARD_BG, corner_radius=12, border_width=1, border_color=BORDER)
        self.app, self.item = app, item
        self.selected = ctk.BooleanVar(value=True)
        self.chapter = ctk.StringVar(value=app.chapter_default(item))
        self.custom_chapter = False
        self.custom_ts: str | None = None   # user-edited timestamps text (split mode)
        self.loading_details = False
        self.path: str | None = None

        self.grid_columnconfigure(3, weight=1)

        self.idx_lbl = ctk.CTkLabel(self, text="1", width=26, text_color=MUTED,
                                    font=ctk.CTkFont(size=13, weight="bold"))
        self.idx_lbl.grid(row=0, column=0, rowspan=3, padx=(10, 0))
        ctk.CTkCheckBox(self, text="", variable=self.selected, width=22, checkbox_width=20, checkbox_height=20,
                        corner_radius=6, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                        command=self._on_select).grid(row=0, column=1, rowspan=3, padx=(4, 8))

        self.thumb = ctk.CTkLabel(self, text="", width=THUMB_W, height=THUMB_H, corner_radius=8,
                                  image=app.placeholder_img, cursor="hand2")
        self.thumb.grid(row=0, column=2, rowspan=3, pady=10)
        self.thumb.bind("<Button-1>", lambda e: webbrowser.open(item.url))
        self.dur_badge = ctk.CTkLabel(self.thumb, text=human_duration(item.duration), fg_color="#000000",
                                      text_color="#FFFFFF", corner_radius=4, height=18,
                                      font=ctk.CTkFont(size=11, weight="bold"))
        if item.duration:
            self.dur_badge.place(relx=1.0, rely=1.0, x=-6, y=-6, anchor="se")

        title = ctk.CTkLabel(self, text=item.title, anchor="w", justify="left", wraplength=260,
                             font=ctk.CTkFont(size=14, weight="bold"))
        title.grid(row=0, column=3, sticky="new", padx=12, pady=(10, 0))
        self.title_lbl = title
        self.bind("<Configure>", self._on_resize)
        meta = [item.channel]
        if item.views is not None:
            meta.append(f"{human_count(item.views)} views")
        if item.upload_date and len(item.upload_date) == 8:
            d = item.upload_date
            meta.append(f"{d[:4]}-{d[4:6]}-{d[6:]}")
        if item.playlist:
            meta.append(f"from “{item.playlist}”")
        ctk.CTkLabel(self, text="  ·  ".join(m for m in meta if m), anchor="w", text_color=MUTED,
                     font=ctk.CTkFont(size=12)).grid(row=1, column=3, sticky="ew", padx=12)

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=2, column=3, sticky="ew", padx=12, pady=(4, 10))
        bottom.grid_columnconfigure(1, weight=1)
        self.chap_lbl = ctk.CTkLabel(bottom, text="Chapter", text_color=MUTED, font=ctk.CTkFont(size=12),
                                     anchor="w", width=140)
        self.chap_entry = ctk.CTkEntry(bottom, textvariable=self.chapter, height=28, corner_radius=6,
                                       border_width=1)
        self.chap_entry.bind("<KeyRelease>", self._on_chapter_edit)
        self.split_lbl = ctk.CTkLabel(bottom, text="", anchor="w", width=140, font=ctk.CTkFont(size=12))
        self.split_btn = ctk.CTkButton(bottom, text="✂  Edit timestamps", height=28, corner_radius=6,
                                       fg_color="transparent", border_width=1, border_color=BORDER,
                                       text_color=("gray10", "gray90"), hover_color=BORDER,
                                       command=lambda: TimestampEditor(app, self))
        self.status = ctk.CTkLabel(bottom, text="Ready", text_color=MUTED, font=ctk.CTkFont(size=12),
                                   anchor="w", width=140)
        self.progress = ctk.CTkProgressBar(bottom, height=6, progress_color=ACCENT)
        self.progress.set(0)
        self.status.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.progress.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(6, 0))
        self.bottom = bottom
        self.show_row(app.row_mode())

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=0, column=4, rowspan=3, padx=(0, 10))
        for txt, cmd, tip in (("▲", lambda: app.move(self, -1), "up"), ("▼", lambda: app.move(self, 1), "down"),
                              ("✕", lambda: app.remove(self), "remove")):
            ctk.CTkButton(btns, text=txt, width=30, height=28, corner_radius=6, fg_color="transparent",
                          hover_color=BORDER, text_color=MUTED, command=cmd).pack(pady=1)

    def _on_resize(self, e):
        # e.width is physical px; CTk wraplength is logical
        s = self._get_widget_scaling()
        wrap = max(160, int(e.width / s) - THUMB_W - 150)
        if wrap != getattr(self, "_wrap", None):
            self._wrap = wrap
            self.title_lbl.configure(wraplength=wrap)

    def show_row(self, mode: str):
        """mode: 'chain' (chapter name entry), 'split' (chapter count + editor) or 'none'."""
        for w in (self.chap_lbl, self.chap_entry, self.split_lbl, self.split_btn):
            w.grid_remove()
        if mode == "chain":
            self.chap_lbl.grid(row=0, column=0, sticky="w", padx=(0, 8))
            self.chap_entry.grid(row=0, column=1, sticky="ew")
        elif mode == "split":
            self.split_lbl.grid(row=0, column=0, sticky="w", padx=(0, 8))
            self.split_btn.grid(row=0, column=1, sticky="w")
            self.refresh_split_label()

    def split_chapters(self) -> list[dict] | None:
        """Chapters that will be used for splitting (None = not known yet)."""
        if self.custom_ts is not None:
            return core.parse_timestamps(self.custom_ts, self.item.duration)
        return self.item.chapters

    def refresh_split_label(self):
        chs = self.split_chapters()
        if self.loading_details:
            txt, col = "✂  reading timestamps…", MUTED
        elif chs is None:
            txt, col = "✂  timestamps read on download", MUTED
        elif not chs:
            txt, col = "✂  no timestamps found", ("#B26A00", "#FFB74D")
        else:
            src = "edited" if self.custom_ts is not None else "chapters"
            txt, col = f"✂  {len(chs)} parts ({src})", ("#2E7D32", "#7BD88F")
        self.split_lbl.configure(text=txt, text_color=col)

    def _on_select(self):
        self.configure(fg_color=CARD_BG)
        self.app.refresh_summary()

    def _on_chapter_edit(self, _e=None):
        self.custom_chapter = True
        self.app.schedule_timestamps()

    def set_status(self, text: str, frac: float | None = None, color=None):
        self.status.configure(text=text, text_color=color or MUTED)
        if frac is not None:
            self.progress.set(max(0.0, min(1.0, frac)))

    def set_thumb(self, img):
        self.thumb.configure(image=img)


# --------------------------------------------------------------------------- #
# Timestamp editor
# --------------------------------------------------------------------------- #
class TimestampEditor(ctk.CTkToplevel):
    """View/edit the timestamps a video will be split at."""

    def __init__(self, app: "App", card: LinkCard):
        super().__init__(app)
        self.app, self.card = app, card
        self.title(f"Timestamps – {card.item.title}")
        self.geometry("620x560")
        self.configure(fg_color=PANEL_BG)
        self.transient(app)
        F = ctk.CTkFont

        ctk.CTkLabel(self, text=card.item.title, font=F(size=16, weight="bold"), anchor="w",
                     wraplength=580, justify="left").pack(fill="x", padx=18, pady=(16, 2))
        ctk.CTkLabel(self, text=f"Length {human_duration(card.item.duration)} · one line per part, e.g. "
                     "“03:45 Song name”. Each part is saved as its own file named after its title.",
                     text_color=MUTED, anchor="w", wraplength=580, justify="left",
                     font=F(size=12)).pack(fill="x", padx=18)

        self.box = ctk.CTkTextbox(self, font=F(family="Consolas", size=13), wrap="none", corner_radius=8,
                                  border_width=1, border_color=BORDER)
        self.box.pack(fill="both", expand=True, padx=18, pady=10)
        self.box.bind("<KeyRelease>", lambda e: self._update_count())

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=18)
        self.count = ctk.CTkLabel(row, text="", anchor="w", font=F(size=12))
        self.count.pack(side="left")

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=18, pady=(8, 16))
        ghost = dict(height=34, corner_radius=8, fg_color="transparent", border_width=1, border_color=BORDER,
                     text_color=("gray10", "gray90"), hover_color=BORDER)
        ctk.CTkButton(btns, text="Video chapters", width=120, command=self._use_chapters, **ghost).pack(side="left")
        ctk.CTkButton(btns, text="From description", width=130, command=self._use_description,
                      **ghost).pack(side="left", padx=6)
        ctk.CTkButton(btns, text="Save", width=110, height=34, corner_radius=8, fg_color=ACCENT,
                      hover_color=ACCENT_HOVER, font=F(size=13, weight="bold"),
                      command=self._save).pack(side="right")
        ctk.CTkButton(btns, text="Cancel", width=90, command=self.destroy, **ghost).pack(side="right", padx=6)

        if card.custom_ts is not None:
            self._set(card.custom_ts)
        elif card.item.chapters is not None:
            self._use_chapters()
        else:
            self._set("")
            self.count.configure(text="Reading timestamps from the video…", text_color=MUTED)
            app.load_details(card, then=lambda: self.winfo_exists() and self._use_chapters())
        self.after(150, self._focus)

    def _focus(self):
        self.lift()
        self.focus_force()
        self.box.focus_set()

    def _set(self, text):
        self.box.delete("1.0", "end")
        self.box.insert("1.0", text)
        self._update_count()

    def _text(self):
        return self.box.get("1.0", "end").strip()

    def _update_count(self):
        chs = core.parse_timestamps(self._text(), self.card.item.duration)
        if chs:
            self.count.configure(text=f"✓ {len(chs)} parts detected", text_color=("#2E7D32", "#7BD88F"))
        else:
            self.count.configure(text="Need at least 2 timestamps (like 00:00 and 03:45)",
                                 text_color=("#B26A00", "#FFB74D"))

    def _use_chapters(self):
        self._set(core.chapters_to_text(self.card.item.chapters or []))

    def _use_description(self):
        chs = core.parse_timestamps(self.card.item.description, self.card.item.duration)
        if chs:
            self._set(core.chapters_to_text(chs))
        else:
            self.count.configure(text="No timestamps found in the description", text_color=("#B26A00", "#FFB74D"))

    def _save(self):
        text = self._text()
        detected = core.chapters_to_text(self.card.item.chapters or [])
        self.card.custom_ts = None if text == detected else text
        self.card.refresh_split_label()
        self.destroy()


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class App(ctk.CTk):
    def __init__(self):
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.title(f"{APP_NAME}")
        self.configure(fg_color=APP_BG)

        self.ffmpeg_dir = core.find_ffmpeg()
        self.js = core.find_js_runtime()
        self.ui_q: queue.Queue = queue.Queue()
        self.cards: list[LinkCard] = []
        self.thumb_cache: dict[str, ctk.CTkImage] = {}
        self.thumb_pool = ThreadPoolExecutor(max_workers=6)
        self.inspect_pool = ThreadPoolExecutor(max_workers=3)
        self.details_pool = ThreadPoolExecutor(max_workers=3)
        self._detail_callbacks: dict[int, list] = {}
        self.pending_inspect = 0
        self._slots: list[list] = []  # [seq, result-or-None] in paste order
        self._seq = 0
        self.busy = False
        self.cancel = threading.Event()
        self._ts_job = None
        self.log_lines: list[str] = []

        self._init_vars()
        self._load_settings()
        ctk.set_appearance_mode(self.v_theme.get())
        ph = placeholder_thumb()
        self.placeholder_img = ctk.CTkImage(ph, ph, size=(THUMB_W, THUMB_H))

        self._build_ui()
        self._fit_window()
        self._on_mode()
        if self.chain_on.get() and self.split_on.get():
            self.split_on.set(False)
        self._on_row_mode_change()
        self.after(80, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.log(f"yt-dlp {yt_dlp.version.__version__} · ffmpeg: {self.ffmpeg_dir or 'NOT FOUND'} · "
                 f"JS runtime: {', '.join(self.js) or 'none'}")
        if not self.ffmpeg_dir:
            self.toast("ffmpeg not found – conversion and chaining will fail", error=True)

    # ------------------------------------------------------------------ state
    def _init_vars(self):
        S, B = ctk.StringVar, ctk.BooleanVar
        self.v_theme = S(value="Dark")
        self.v_mode = S(value="Video")
        self.v_quality = S(value="1080p")
        self.v_container = S(value="mp4")
        self.v_compat = B(value=True)
        self.v_afmt = S(value="mp3")
        self.v_abr = S(value="192")
        self.v_outdir = S(value=str(Path.home() / "Downloads" / APP_NAME))
        self.v_expand = B(value=True)
        self.v_limit = S(value="200")
        self.v_thumb = B(value=True)
        self.v_meta = B(value=True)
        self.v_subs = B(value=False)
        self.v_sublangs = S(value="en")
        self.v_infojson = B(value=False)
        self.v_archive = B(value=False)
        self.v_rate = S(value="")
        self.v_cookies = S(value="None")
        self.chain_on = B(value=False)
        self.v_chain_name = S(value="My Mix")
        self.v_clean = B(value=True)
        self.v_keep_parts = B(value=False)
        self.split_on = B(value=False)
        self.v_split_names = S(value=core.SPLIT_NAME_STYLES[0])
        self.v_keep_full = B(value=False)

    def _persisted(self):
        return {k: getattr(self, k) for k in (
            "v_theme", "v_mode", "v_quality", "v_container", "v_compat", "v_afmt", "v_abr", "v_outdir",
            "v_expand", "v_limit", "v_thumb", "v_meta", "v_subs", "v_sublangs", "v_infojson", "v_archive",
            "v_rate", "v_cookies", "chain_on", "v_chain_name", "v_clean", "v_keep_parts",
            "split_on", "v_split_names", "v_keep_full")}

    def _load_settings(self):
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            for k, var in self._persisted().items():
                if k in data:
                    var.set(data[k])
        except Exception:
            pass

    def _save_settings(self):
        try:
            SETTINGS_FILE.write_text(json.dumps({k: v.get() for k, v in self._persisted().items()}, indent=2),
                                     encoding="utf-8")
        except Exception:
            pass

    def _fit_window(self):
        # CTk geometry is in logical px; winfo_screen* reports logical px too.
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = int(min(1320, sw * 0.9)), int(min(860, sh * 0.86))
        self.geometry(f"{w}x{h}+{int((sw - w) / 2)}+{int(max(0, (sh - h) / 3))}")
        self.minsize(min(980, w), min(620, h))

    # ------------------------------------------------------------------ layout
    def _build_ui(self):
        F = ctk.CTkFont
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # header
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 8))
        head.grid_columnconfigure(1, weight=1)
        logo = ctk.CTkLabel(head, text="▶", width=38, height=38, corner_radius=10, fg_color=ACCENT,
                            text_color="white", font=F(size=18, weight="bold"))
        logo.grid(row=0, column=0, rowspan=2, padx=(0, 12))
        ctk.CTkLabel(head, text=APP_NAME, font=F(size=22, weight="bold"), anchor="w").grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(head, text="Download YouTube video & audio · chain clips into one file · split videos by timestamps",
                     text_color=MUTED, anchor="w", font=F(size=12)).grid(row=1, column=1, sticky="w")
        ctk.CTkSegmentedButton(head, values=["Light", "Dark", "System"], variable=self.v_theme,
                               command=lambda v: ctk.set_appearance_mode(v)).grid(row=0, column=2, rowspan=2)

        # URL bar
        bar = ctk.CTkFrame(self, fg_color=PANEL_BG, corner_radius=14, border_width=1, border_color=BORDER)
        bar.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 12))
        bar.grid_columnconfigure(0, weight=1)
        self.url_entry = ctk.CTkEntry(bar, height=42, corner_radius=10, border_width=0, fg_color="transparent",
                                      font=F(size=14),
                                      placeholder_text="Paste YouTube links, Shorts, playlists or channels "
                                                       "(separate several with spaces) and press Enter")
        self.url_entry.grid(row=0, column=0, sticky="ew", padx=(10, 6), pady=6)
        self.url_entry.bind("<Return>", lambda e: self.add_links())
        ctk.CTkButton(bar, text="Paste", width=80, height=36, corner_radius=8, fg_color="transparent",
                      border_width=1, border_color=BORDER, text_color=("gray10", "gray90"), hover_color=BORDER,
                      command=self._paste).grid(row=0, column=1, padx=4)
        self.add_btn = ctk.CTkButton(bar, text="＋ Add & Preview", width=150, height=36, corner_radius=8,
                                     fg_color=ACCENT, hover_color=ACCENT_HOVER, font=F(size=13, weight="bold"),
                                     command=self.add_links)
        self.add_btn.grid(row=0, column=2, padx=(4, 8))

        # body
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew", padx=20)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        # -- library
        lib = ctk.CTkFrame(body, fg_color=PANEL_BG, corner_radius=14, border_width=1, border_color=BORDER)
        lib.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        lib.grid_columnconfigure(0, weight=1)
        lib.grid_rowconfigure(1, weight=1)
        lh = ctk.CTkFrame(lib, fg_color="transparent")
        lh.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
        lh.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(lh, text="Preview", font=F(size=16, weight="bold")).grid(row=0, column=0, sticky="w")
        self.summary = ctk.CTkLabel(lh, text="No links yet", text_color=MUTED, font=F(size=12))
        self.summary.grid(row=0, column=1, sticky="w", padx=12)
        for i, (t, cmd) in enumerate((("Select all", lambda: self._select_all(True)),
                                      ("None", lambda: self._select_all(False)),
                                      ("Clear", self.clear_all))):
            ctk.CTkButton(lh, text=t, width=70, height=28, corner_radius=6, fg_color="transparent",
                          hover_color=BORDER, text_color=("gray20", "gray80"), command=cmd
                          ).grid(row=0, column=2 + i, padx=2)
        self.list_frame = ctk.CTkScrollableFrame(lib, fg_color="transparent", corner_radius=0)
        self.list_frame.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 8))
        self.list_frame.grid_columnconfigure(0, weight=1)
        self.empty = ctk.CTkLabel(self.list_frame, text="Paste a link above to see a preview here.\n\n"
                                  "Playlists and channels are expanded into individual videos —\n"
                                  "tick the ones you want, reorder them, and download.",
                                  text_color=MUTED, font=F(size=13), justify="center")
        self.empty.grid(row=0, column=0, pady=80)

        # -- sidebar
        side = ctk.CTkFrame(body, width=350, fg_color=PANEL_BG, corner_radius=14, border_width=1,
                            border_color=BORDER)
        side.grid(row=0, column=1, sticky="ns")
        side.grid_propagate(False)
        side.grid_rowconfigure(0, weight=1)
        side.grid_columnconfigure(0, weight=1)
        tabs = ctk.CTkTabview(side, fg_color=PANEL_BG, segmented_button_selected_color=ACCENT,
                              segmented_button_selected_hover_color=ACCENT_HOVER)
        tabs.grid(row=0, column=0, sticky="nsew", padx=8, pady=(4, 8))
        self._build_format_tab(self._scroll(tabs.add("Format")))
        self._build_chain_tab(self._scroll(tabs.add("Chain")))
        self._build_split_tab(self._scroll(tabs.add("Split")))
        self._build_options_tab(self._scroll(tabs.add("Options")))
        self.tabs = tabs

        # footer
        foot = ctk.CTkFrame(self, fg_color=PANEL_BG, corner_radius=14, border_width=1, border_color=BORDER)
        foot.grid(row=3, column=0, sticky="ew", padx=20, pady=12)
        foot.grid_columnconfigure(1, weight=1)
        self.log_btn = ctk.CTkButton(foot, text="Log", width=60, height=36, corner_radius=8,
                                     fg_color="transparent", hover_color=BORDER, text_color=("gray20", "gray80"),
                                     command=self.show_log)
        self.log_btn.grid(row=0, column=0, rowspan=2, padx=(10, 4), pady=10)
        self.status = ctk.CTkLabel(foot, text="Ready", anchor="w", font=F(size=13))
        self.status.grid(row=0, column=1, sticky="ew", padx=8, pady=(10, 0))
        self.overall = ctk.CTkProgressBar(foot, height=8, progress_color=ACCENT)
        self.overall.set(0)
        self.overall.grid(row=1, column=1, sticky="ew", padx=8, pady=(4, 12))
        ctk.CTkButton(foot, text="Open folder", width=110, height=40, corner_radius=8, fg_color="transparent",
                      border_width=1, border_color=BORDER, text_color=("gray10", "gray90"), hover_color=BORDER,
                      command=self._open_folder).grid(row=0, column=2, rowspan=2, padx=4)
        self.cancel_btn = ctk.CTkButton(foot, text="Cancel", width=90, height=40, corner_radius=8,
                                        fg_color="transparent", border_width=1, border_color=BORDER,
                                        text_color=("gray10", "gray90"), hover_color=BORDER,
                                        state="disabled", command=self._cancel)
        self.cancel_btn.grid(row=0, column=3, rowspan=2, padx=4)
        self.go_btn = ctk.CTkButton(foot, text="⬇  Download", width=190, height=40, corner_radius=8,
                                    fg_color=ACCENT, hover_color=ACCENT_HOVER, font=F(size=14, weight="bold"),
                                    command=self.start)
        self.go_btn.grid(row=0, column=4, rowspan=2, padx=(4, 10))

    def _scroll(self, tab):
        f = ctk.CTkScrollableFrame(tab, fg_color=PANEL_BG, corner_radius=0)
        f.pack(fill="both", expand=True)
        return f

    def _section(self, parent, text):
        ctk.CTkLabel(parent, text=text.upper(), text_color=MUTED, anchor="w",
                     font=ctk.CTkFont(size=11, weight="bold")).pack(fill="x", padx=6, pady=(12, 4))

    def _row(self, parent, label, widget_factory):
        r = ctk.CTkFrame(parent, fg_color="transparent")
        r.pack(fill="x", padx=6, pady=3)
        ctk.CTkLabel(r, text=label, anchor="w").pack(side="left")
        w = widget_factory(r)
        w.pack(side="right")
        return w

    def _switch(self, parent, text, var, command=None):
        s = ctk.CTkSwitch(parent, text=text, variable=var, progress_color=ACCENT, command=command)
        s.pack(fill="x", padx=6, pady=4)
        return s

    def _build_format_tab(self, t):
        self._section(t, "Download as")
        ctk.CTkSegmentedButton(t, values=["Video", "Audio"], variable=self.v_mode, height=34,
                               selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
                               command=lambda v: self._on_mode()).pack(fill="x", padx=6)
        self.vid_box = ctk.CTkFrame(t, fg_color="transparent")
        self._row(self.vid_box, "Max quality", lambda p: ctk.CTkOptionMenu(p, values=VIDEO_QUALITIES,
                  variable=self.v_quality, width=120))
        self._row(self.vid_box, "Container", lambda p: ctk.CTkOptionMenu(p, values=VIDEO_CONTAINERS,
                  variable=self.v_container, width=120))
        self._switch(self.vid_box, "Max compatibility (H.264 + AAC)", self.v_compat)
        self.aud_box = ctk.CTkFrame(t, fg_color="transparent")
        self._row(self.aud_box, "Format", lambda p: ctk.CTkOptionMenu(p, values=AUDIO_FORMATS,
                  variable=self.v_afmt, width=120, command=lambda v: self._on_mode()))
        self._row(self.aud_box, "Bitrate (kbps)", lambda p: ctk.CTkOptionMenu(p, values=AUDIO_BITRATES,
                  variable=self.v_abr, width=120))
        self.fmt_anchor = ctk.CTkFrame(t, fg_color="transparent", height=1)
        self.fmt_anchor.pack(fill="x")

        self._section(t, "Embed")
        self._switch(t, "Cover art / thumbnail", self.v_thumb)
        self._switch(t, "Metadata + chapters", self.v_meta)
        sr = ctk.CTkFrame(t, fg_color="transparent")
        sr.pack(fill="x")
        ctk.CTkSwitch(sr, text="Subtitles", variable=self.v_subs, progress_color=ACCENT).pack(side="left", padx=6, pady=4)
        ctk.CTkEntry(sr, textvariable=self.v_sublangs, width=90, height=28,
                     placeholder_text="en,es").pack(side="right", padx=6)

        self._section(t, "Save to")
        of = ctk.CTkFrame(t, fg_color="transparent")
        of.pack(fill="x", padx=6)
        of.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(of, textvariable=self.v_outdir, height=32).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(of, text="…", width=36, height=32, command=self._browse).grid(row=0, column=1, padx=(6, 0))

    def _build_chain_tab(self, t):
        F = ctk.CTkFont
        self._section(t, "Chain into one file")
        self._switch(t, "Chain selected items", self.chain_on, command=self._on_chain_toggle)
        ctk.CTkLabel(t, text="Downloads every ticked item, then joins them in list order into a single "
                     "file with one chapter per item.", text_color=MUTED, wraplength=280, justify="left",
                     anchor="w", font=F(size=12)).pack(fill="x", padx=6)
        self._row(t, "File name", lambda p: ctk.CTkEntry(p, textvariable=self.v_chain_name, width=180))
        self._switch(t, "Clean titles (drop “Official Video”…)", self.v_clean, command=self._on_clean_toggle)
        self._switch(t, "Keep individual files", self.v_keep_parts)

        hdr = ctk.CTkFrame(t, fg_color="transparent")
        hdr.pack(fill="x", padx=6, pady=(12, 4))
        ctk.CTkLabel(hdr, text="TIMESTAMPS", text_color=MUTED, font=F(size=11, weight="bold")).pack(side="left")
        ctk.CTkButton(hdr, text="Copy", width=60, height=24, corner_radius=6, fg_color="transparent",
                      border_width=1, border_color=BORDER, text_color=("gray20", "gray80"), hover_color=BORDER,
                      command=self._copy_timestamps).pack(side="right")
        self.ts_box = ctk.CTkTextbox(t, height=200, corner_radius=8, border_width=1, border_color=BORDER,
                                     font=ctk.CTkFont(family="Consolas", size=12), wrap="none")
        self.ts_box.pack(fill="x", padx=6, pady=(0, 4))
        self.ts_note = ctk.CTkLabel(t, text="Estimated from video lengths; exact values are written "
                                    "after chaining.", text_color=MUTED, wraplength=280, justify="left",
                                    anchor="w", font=F(size=11))
        self.ts_note.pack(fill="x", padx=6)

    def _build_split_tab(self, t):
        F = ctk.CTkFont
        self._section(t, "Split by timestamps")
        self._switch(t, "Split into parts", self.split_on, command=self._on_split_toggle)
        ctk.CTkLabel(t, text="Reads each video's chapters, or the timestamps in its description, and saves "
                     "one file per part. Parts are named after their timestamp titles and saved in a "
                     "folder named after the video.", text_color=MUTED, wraplength=280, justify="left",
                     anchor="w", font=F(size=12)).pack(fill="x", padx=6)
        self._row(t, "Part names", lambda p: ctk.CTkOptionMenu(p, values=core.SPLIT_NAME_STYLES,
                  variable=self.v_split_names, width=170))
        self._switch(t, "Keep the full file too", self.v_keep_full)
        self._section(t, "Example")
        self.split_example = ctk.CTkLabel(t, text="", anchor="w", justify="left",
                                          font=F(family="Consolas", size=12))
        self.split_example.pack(fill="x", padx=6)
        self.v_split_names.trace_add("write", lambda *a: self._render_split_example())
        self._render_split_example()
        self._section(t, "Tips")
        ctk.CTkLabel(t, text="• Use “✂ Edit timestamps” on a card to fix titles, or to paste your own "
                     "timestamps (for example from a pinned comment).\n"
                     "• Audio is cut without re-encoding, so there is no quality loss.\n"
                     "• Video parts are re-encoded so every cut is frame-accurate.",
                     text_color=MUTED, wraplength=280, justify="left", anchor="w",
                     font=F(size=12)).pack(fill="x", padx=6)

    def _render_split_example(self):
        style = self.v_split_names.get()
        ext = self.v_afmt.get() if self.v_mode.get() == "Audio" else "mp4"
        chs = [{"start": 0, "title": "Intro"}, {"start": 225, "title": "First Song"},
               {"start": 432, "title": "Second Song"}]
        lines = ["📁 Video Title/"] + [f"   {core._part_name(style, i, 3, c)}.{ext}" for i, c in enumerate(chs, 1)]
        self.split_example.configure(text="\n".join(lines))

    def _build_options_tab(self, t):
        self._section(t, "Links")
        self._switch(t, "Expand playlists & channels", self.v_expand)
        self._row(t, "Max items per playlist", lambda p: ctk.CTkEntry(p, textvariable=self.v_limit, width=70))
        self._section(t, "Files")
        self._switch(t, "Save info .json + description", self.v_infojson)
        self._switch(t, "Skip already downloaded (archive)", self.v_archive)
        self._section(t, "Network")
        self._row(t, "Rate limit MB/s", lambda p: ctk.CTkEntry(p, textvariable=self.v_rate, width=70,
                                                              placeholder_text="none"))
        self._row(t, "Cookies from", lambda p: ctk.CTkOptionMenu(
            p, values=["None", "Chrome", "Edge", "Firefox", "Brave", "Opera"], variable=self.v_cookies, width=120))
        ctk.CTkLabel(t, text="Use browser cookies for age-restricted or members-only videos you can watch.",
                     text_color=MUTED, wraplength=280, justify="left", anchor="w",
                     font=ctk.CTkFont(size=11)).pack(fill="x", padx=6)
        self._section(t, "Maintenance")
        ctk.CTkButton(t, text="Update yt-dlp", height=32, fg_color="transparent", border_width=1,
                      border_color=BORDER, text_color=("gray10", "gray90"), hover_color=BORDER,
                      command=self._update_ytdlp).pack(fill="x", padx=6)
        ctk.CTkLabel(t, text=f"yt-dlp {yt_dlp.version.__version__}", text_color=MUTED,
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=6, pady=4)

    # ------------------------------------------------------------------ helpers
    def log(self, msg: str):
        msg = str(msg).rstrip()
        self.log_lines.append(msg)
        del self.log_lines[:-3000]
        if getattr(self, "log_box", None) and self.log_box.winfo_exists():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

    def post(self, kind, *args):
        self.ui_q.put((kind, args))

    def toast(self, text, error=False):
        self.status.configure(text=text, text_color=("#C62828", "#FF8A80") if error else ("gray10", "gray90"))

    def chapter_default(self, item: LinkItem) -> str:
        return core.clean_title(item.title) if self.v_clean.get() else item.title

    def show_log(self):
        if getattr(self, "log_win", None) and self.log_win.winfo_exists():
            self.log_win.focus()
            return
        w = ctk.CTkToplevel(self)
        w.title(f"{APP_NAME} – Log")
        w.geometry("820x420")
        self.log_win = w
        self.log_box = ctk.CTkTextbox(w, font=ctk.CTkFont(family="Consolas", size=12), wrap="word")
        self.log_box.pack(fill="both", expand=True, padx=10, pady=10)
        self.log_box.insert("end", "\n".join(self.log_lines) + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        w.after(100, w.lift)

    def _paste(self):
        try:
            txt = self.clipboard_get()
        except Exception:
            return
        cur = self.url_entry.get().strip()
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, (cur + " " if cur else "") + " ".join(txt.split()))

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.v_outdir.get() or str(Path.home()))
        if d:
            self.v_outdir.set(d)

    def _open_folder(self):
        d = self.v_outdir.get()
        os.makedirs(d, exist_ok=True)
        core.open_path(d)

    def _on_mode(self):
        for b in (self.vid_box, self.aud_box):
            b.pack_forget()
        box = self.vid_box if self.v_mode.get() == "Video" else self.aud_box
        box.pack(fill="x", pady=(6, 0), before=self.fmt_anchor)
        if hasattr(self, "split_example"):
            self._render_split_example()
        self.schedule_timestamps()

    def row_mode(self) -> str:
        if self.chain_on.get():
            return "chain"
        if self.split_on.get():
            return "split"
        return "none"

    def _on_row_mode_change(self):
        mode = self.row_mode()
        for c in self.cards:
            c.show_row(mode)
        self.go_btn.configure(text={"chain": "⛓  Download & Chain", "split": "✂  Download & Split"}
                              .get(mode, "⬇  Download"))
        if mode == "split":
            for c in self.cards:
                if c.item.chapters is None:
                    self.load_details(c)
        self.schedule_timestamps()

    def _on_chain_toggle(self):
        if self.chain_on.get() and self.split_on.get():
            self.split_on.set(False)
            self.toast("Split turned off – chaining and splitting can't be combined")
        self._on_row_mode_change()

    def _on_split_toggle(self):
        if self.split_on.get() and self.chain_on.get():
            self.chain_on.set(False)
            self.toast("Chain turned off – chaining and splitting can't be combined")
        self._on_row_mode_change()

    def load_details(self, card: LinkCard, then=None):
        """Fetch full info (chapters + description) for a card in the background."""
        key = id(card)
        if then:
            self._detail_callbacks.setdefault(key, []).append(then)
        if card.loading_details:
            return
        card.loading_details = True
        card.refresh_split_label()
        cookies = None if self.v_cookies.get() == "None" else self.v_cookies.get()

        def work():
            try:
                d = core.fetch_details(card.item.url, self.js, cookies)
                self.post("details", card, d, None)
            except Exception as e:
                self.post("details", card, None, str(e))

        self.details_pool.submit(work)

    def _on_clean_toggle(self):
        for c in self.cards:
            if not c.custom_chapter:
                c.chapter.set(self.chapter_default(c.item))
        self.schedule_timestamps()

    def _copy_timestamps(self):
        self.clipboard_clear()
        self.clipboard_append(self.ts_box.get("1.0", "end").strip())
        self.toast("Timestamps copied to clipboard")

    # ------------------------------------------------------------------ links
    def add_links(self):
        urls = [u for u in self.url_entry.get().split() if u.strip()]
        if not urls:
            self.toast("Paste a link first", error=True)
            return
        self.url_entry.delete(0, "end")
        try:
            limit = max(1, int(self.v_limit.get()))
        except ValueError:
            limit = 200
        cookies = None if self.v_cookies.get() == "None" else self.v_cookies.get()
        for url in urls:
            self.pending_inspect += 1
            self._seq += 1
            self._slots.append([self._seq, None])
            self.inspect_pool.submit(self._inspect_worker, self._seq, url, self.v_expand.get(), limit, cookies)
        self._update_inspect_status()

    def _inspect_worker(self, seq, url, expand, limit, cookies):
        try:
            items = core.inspect_link(url, self.js, expand, limit, cookies)
            self.post("items", seq, items, url)
        except Exception as e:
            self.post("inspect_err", seq, url, str(e))

    def _resolve_slot(self, seq, items):
        """Store a finished lookup and add results to the list in paste order."""
        for slot in self._slots:
            if slot[0] == seq:
                slot[1] = items
        added = 0
        while self._slots and self._slots[0][1] is not None:
            added += self._add_items(self._slots.pop(0)[1])
        return added

    def _update_inspect_status(self):
        if self.pending_inspect > 0:
            self.toast(f"Loading preview for {self.pending_inspect} link(s)…")
            self.add_btn.configure(text="Loading…")
        else:
            self.add_btn.configure(text="＋ Add & Preview")
            self.refresh_summary()

    def _add_items(self, items: list[LinkItem]):
        existing = {c.item.url for c in self.cards}
        if self.empty.winfo_ismapped():
            self.empty.grid_forget()
        added = 0
        for it in items:
            if not it.url or it.url in existing:
                continue
            existing.add(it.url)
            card = LinkCard(self.list_frame, self, it)
            self.cards.append(card)
            card.grid(row=len(self.cards), column=0, sticky="ew", padx=6, pady=5)
            self._load_thumb(card)
            if self.split_on.get() and it.chapters is None:
                self.load_details(card)
            added += 1
        self._renumber()
        self.schedule_timestamps()
        return added

    def _load_thumb(self, card: LinkCard):
        url = card.item.thumbnail
        if not url:
            return
        if url in self.thumb_cache:
            card.set_thumb(self.thumb_cache[url])
            return

        def fetch():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    img = Image.open(io.BytesIO(r.read())).convert("RGB")
                # center-crop to 16:9 and round corners
                w, h = img.size
                tw = min(w, int(h * 16 / 9))
                th = int(tw * 9 / 16)
                img = img.crop(((w - tw) // 2, (h - th) // 2, (w + tw) // 2, (h + th) // 2))
                img = img.resize((THUMB_W * 2, THUMB_H * 2), Image.LANCZOS)
                mask = Image.new("L", img.size, 0)
                ImageDraw.Draw(mask).rounded_rectangle((0, 0, *img.size), radius=16, fill=255)
                img.putalpha(mask)
                self.post("thumb", card, url, img)
            except Exception:
                pass

        self.thumb_pool.submit(fetch)

    def _renumber(self):
        for i, c in enumerate(self.cards):
            c.idx_lbl.configure(text=str(i + 1))
            c.grid_configure(row=i + 1)

    def move(self, card: LinkCard, delta: int):
        if self.busy:
            return
        i = self.cards.index(card)
        j = i + delta
        if 0 <= j < len(self.cards):
            self.cards[i], self.cards[j] = self.cards[j], self.cards[i]
            self._renumber()
            self.schedule_timestamps()

    def remove(self, card: LinkCard):
        if self.busy:
            return
        self.cards.remove(card)
        card.destroy()
        self._renumber()
        self.refresh_summary()
        self.schedule_timestamps()
        if not self.cards:
            self.empty.grid(row=0, column=0, pady=80)

    def clear_all(self):
        if self.busy:
            return
        for c in self.cards:
            c.destroy()
        self.cards.clear()
        self.empty.grid(row=0, column=0, pady=80)
        self.refresh_summary()
        self.schedule_timestamps()

    def _select_all(self, val: bool):
        for c in self.cards:
            c.selected.set(val)
        self.refresh_summary()

    def selected_cards(self):
        return [c for c in self.cards if c.selected.get()]

    def refresh_summary(self):
        sel = self.selected_cards()
        if not self.cards:
            self.summary.configure(text="No links yet")
        else:
            total = sum(c.item.duration or 0 for c in sel)
            self.summary.configure(text=f"{len(sel)} of {len(self.cards)} selected · {human_duration(total)} total")
        if not self.busy and self.pending_inspect == 0:
            self.toast("Ready" if self.cards else "Ready – paste a link to begin")
        self.schedule_timestamps()

    def schedule_timestamps(self):
        if self._ts_job:
            self.after_cancel(self._ts_job)
        self._ts_job = self.after(150, self._render_timestamps)

    def _render_timestamps(self):
        self._ts_job = None
        sel = self.selected_cards()
        stamps = core.build_timestamps([(c.chapter.get().strip() or c.item.title, c.item.duration or 0) for c in sel])
        total = sum(c.item.duration or 0 for c in sel)
        txt = core.format_timestamps(stamps, total) if sel else "Tick items in the preview list to\nbuild a chapter list."
        self._set_ts(txt)

    def _set_ts(self, txt):
        self.ts_box.configure(state="normal")
        self.ts_box.delete("1.0", "end")
        self.ts_box.insert("1.0", txt)

    # ------------------------------------------------------------------ run
    def _gather_options(self) -> dict:
        return {
            "mode": self.v_mode.get().lower(),
            "video_quality": self.v_quality.get(), "container": self.v_container.get(),
            "compat": self.v_compat.get(), "audio_format": self.v_afmt.get(), "audio_bitrate": self.v_abr.get(),
            "out_dir": self.v_outdir.get(), "thumbnail": self.v_thumb.get(), "metadata": self.v_meta.get(),
            "subs": self.v_subs.get(), "sub_langs": self.v_sublangs.get(), "info_json": self.v_infojson.get(),
            "archive": self.v_archive.get(), "rate_limit": self.v_rate.get().strip(),
            "cookies_browser": self.v_cookies.get(),
        }

    def start(self):
        if self.busy:
            return
        sel = self.selected_cards()
        if not sel:
            self.toast("Nothing selected – add links and tick the ones to download", error=True)
            return
        chain = self.chain_on.get()
        split = self.split_on.get() and not chain
        if chain and len(sel) < 2:
            self.toast("Select at least 2 items to chain", error=True)
            return
        o = self._gather_options()
        try:
            os.makedirs(o["out_dir"], exist_ok=True)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Cannot create output folder:\n{e}")
            return
        name = core.safe_filename(self.v_chain_name.get() or "My Mix")
        if chain:
            o["archive"] = False  # we need every part's file path
            o["subs"] = False
            o["subfolder"] = f"{name} - parts"
        if split:
            o["archive"] = False  # need the file path to split it
        self._save_settings()
        jobs = [(c, c.item.url, c.chapter.get().strip() or c.item.title) for c in sel]
        split_cfg = {"names": self.v_split_names.get(), "keep_full": self.v_keep_full.get(),
                     "custom": {id(c): c.custom_ts for c in sel}} if split else None
        for c in self.cards:
            c.set_status("Queued" if c in sel else "Skipped", 0)
        self.busy = True
        self.cancel.clear()
        self.go_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.overall.set(0)
        threading.Thread(target=self._run, args=(jobs, o, chain, name, split_cfg), daemon=True).start()

    def _cancel(self):
        self.cancel.set()
        self.toast("Cancelling…")

    def _run(self, jobs, o, chain, name, split_cfg=None):
        opts = core.build_ydl_opts(o, self.ffmpeg_dir, self.js)
        app = self

        class Logger:
            def debug(self, m):
                if not m.startswith("[debug]") and not (m.startswith("[download]") and "%" in m):
                    app.post("log", m)

            def info(self, m):
                app.post("log", m)

            def warning(self, m):
                if "impersonat" not in m:
                    app.post("log", "WARNING: " + m)

            def error(self, m):
                app.post("log", m)

        n = len(jobs)
        weight = 0.8 if chain else 1.0
        dl_share = 0.6 if split_cfg else 1.0  # per item: download share vs. split share
        done_parts, errors, split_count = [], 0, 0
        for k, (card, url, chap) in enumerate(jobs):
            if self.cancel.is_set():
                break
            self.post("card", card, "Starting…", 0, None)
            self.post("status", f"Downloading {k + 1}/{n}: {card.item.title}")

            def prog(d, card=card, k=k):
                st = d.get("status")
                if st == "downloading":
                    tot = d.get("total_bytes") or d.get("total_bytes_estimate")
                    frac = d.get("downloaded_bytes", 0) / tot if tot else 0
                    spd = f" · {human_bytes(d.get('speed'))}/s" if d.get("speed") else ""
                    self.post("card", card, f"{frac * 100:.0f}%{spd}", frac * dl_share, None)
                    self.post("overall", weight * (k + frac * dl_share) / n)
                elif st == "finished":
                    self.post("card", card, "Processing…", 1, None)
                elif st == "postprocess" and d.get("pp_status") == "started":
                    self.post("card", card, f"{(d.get('pp') or 'Post')[:18]}…", None, None)

            try:
                path, info = core.download_one(url, opts, self.cancel, prog, Logger())
                if path and split_cfg:
                    split_count += self._split_one(card, path, info, o, split_cfg, k, n)
                elif path:
                    done_parts.append((card, path, chap))
                    self.post("card", card, "Done ✓", 1, ("#2E7D32", "#7BD88F"))
                else:
                    self.post("card", card, "Skipped (archived)", 1, None)
            except Cancelled:
                self.post("card", card, "Cancelled", 0, None)
                break
            except Exception as e:
                if self.cancel.is_set():
                    self.post("card", card, "Cancelled", 0, None)
                    break
                errors += 1
                self.post("card", card, "Error", 0, ("#C62828", "#FF8A80"))
                self.post("log", f"ERROR ({url}): {e}")
            self.post("overall", weight * (k + 1) / n)

        result_msg = None
        if chain and not self.cancel.is_set():
            if len(done_parts) < 2:
                result_msg = ("Chaining needs at least 2 successful downloads", True)
            else:
                ext = o["audio_format"] if o["mode"] == "audio" else ("mkv" if o["container"] == "mkv" else "mp4")
                out = os.path.join(o["out_dir"], f"{name}.{ext}")
                i = 2
                while os.path.exists(out):
                    out = os.path.join(o["out_dir"], f"{name} ({i}).{ext}")
                    i += 1
                self.post("status", f"Chaining {len(done_parts)} files into {os.path.basename(out)}…")
                for c, _, _ in done_parts:
                    self.post("card", c, "Chaining…", None, None)
                max_h = None if o["video_quality"] == "Best" else int(o["video_quality"].rstrip("p"))
                try:
                    _, ts = core.chain_files(
                        self.ffmpeg_dir, [(p, t) for _, p, t in done_parts], out, o["mode"],
                        o["audio_format"], o["audio_bitrate"], max_h, self.cancel,
                        on_progress=lambda f: self.post("overall", 0.8 + 0.2 * f),
                        log=lambda m: self.post("log", m))
                    self.post("ts_final", ts)
                    for c, _, _ in done_parts:
                        self.post("card", c, "Chained ✓", 1, ("#2E7D32", "#7BD88F"))
                    if not self.v_keep_parts.get():
                        parts_dir = os.path.join(o["out_dir"], o["subfolder"])
                        for _, p, _ in done_parts:
                            try:
                                os.remove(p)
                            except OSError:
                                pass
                        try:
                            for f in os.listdir(parts_dir):  # leftover thumbnails etc.
                                os.remove(os.path.join(parts_dir, f))
                            os.rmdir(parts_dir)
                        except OSError:
                            pass
                    result_msg = (f"Created {os.path.basename(out)} with {len(done_parts)} chapters"
                                  + (f" · {errors} failed" if errors else ""), False)
                except Cancelled:
                    pass
                except Exception as e:
                    self.post("log", f"Chain error: {e}\n{traceback.format_exc()}")
                    result_msg = ("Chaining failed – see Log", True)
        if self.cancel.is_set():
            result_msg = ("Cancelled", True)
        elif result_msg is None and split_cfg:
            result_msg = (f"Finished · {split_count} parts saved" + (f" · {errors} failed" if errors else ""),
                          errors > 0)
        elif result_msg is None:
            ok = len(done_parts)
            result_msg = (f"Finished · {ok} downloaded" + (f" · {errors} failed" if errors else ""), errors > 0)
        self.post("finished", *result_msg)

    def _split_one(self, card, path, info, o, cfg, k, n) -> int:
        """Split one downloaded file into its timestamped parts. Returns the part count."""
        custom = cfg["custom"].get(id(card))
        dur = info.get("duration") or card.item.duration
        chs = core.parse_timestamps(custom, dur) if custom is not None else core.chapters_from_info(info)
        if not chs:
            self.post("card", card, "No timestamps – kept whole", 1, ("#B26A00", "#FFB74D"))
            self.post("log", f"No chapters/timestamps for {info.get('title')}; file kept as is.")
            return 0
        title = info.get("title") or card.item.title
        dest = os.path.join(o["out_dir"], core.safe_filename(title))
        self.post("card", card, f"Splitting into {len(chs)}…", 0.6, None)
        self.post("status", f"Splitting “{title}” into {len(chs)} parts…")
        self.post("log", f"Splitting {os.path.basename(path)} → {dest}")

        def prog(f):
            self.post("card", card, f"Splitting {int(f * len(chs))}/{len(chs)}…", 0.6 + 0.4 * f, None)
            self.post("overall", (k + 0.6 + 0.4 * f) / n)

        outs = core.split_file(self.ffmpeg_dir, path, chs, dest, o["mode"], cfg["names"],
                               meta={"album": title, "artist": info.get("artist") or info.get("channel")
                                     or info.get("uploader")},
                               cancel=self.cancel, on_progress=prog, log=lambda m: self.post("log", m))
        if cfg["keep_full"]:
            try:  # keep the full file alongside its parts
                os.replace(path, os.path.join(dest, os.path.basename(path)))
            except OSError:
                pass
        else:
            try:
                os.remove(path)
            except OSError:
                pass
        self.post("card", card, f"Split ✓ {len(outs)} parts", 1, ("#2E7D32", "#7BD88F"))
        return len(outs)

    # ------------------------------------------------------------------ ui pump
    def _drain(self):
        try:
            for _ in range(400):
                kind, a = self.ui_q.get_nowait()
                if kind == "log":
                    self.log(a[0])
                elif kind == "card":
                    card, text, frac, color = a
                    if card.winfo_exists():
                        card.set_status(text, frac, color)
                elif kind == "overall":
                    self.overall.set(a[0])
                elif kind == "status":
                    self.toast(a[0])
                elif kind == "thumb":
                    card, url, img = a
                    if url not in self.thumb_cache:
                        self.thumb_cache[url] = ctk.CTkImage(img, img, size=(THUMB_W, THUMB_H))
                    if card.winfo_exists():
                        card.set_thumb(self.thumb_cache[url])
                elif kind == "items":
                    seq, items, url = a
                    self.pending_inspect -= 1
                    self.log(f"Loaded {len(items)} item(s) from {url}")
                    self._resolve_slot(seq, items)
                    self._update_inspect_status()
                elif kind == "inspect_err":
                    seq, url, err = a
                    self.pending_inspect -= 1
                    self._resolve_slot(seq, [])
                    self.log(f"Could not load {url}: {err}")
                    self._update_inspect_status()
                    self.toast(f"Could not load {url[:60]} – see Log", error=True)
                elif kind == "details":
                    card, d, err = a
                    card.loading_details = False
                    if d is not None:
                        card.item.chapters = d.chapters
                        card.item.description = d.description
                        if not card.item.duration:
                            card.item.duration = d.duration
                    else:
                        card.item.chapters = []
                        self.log(f"Could not read timestamps for {card.item.url}: {err}")
                    if card.winfo_exists():
                        card.refresh_split_label()
                    for cb in self._detail_callbacks.pop(id(card), []):
                        try:
                            cb()
                        except Exception:
                            pass
                elif kind == "ts_final":
                    self._set_ts(a[0])
                    self.ts_note.configure(text="Exact timestamps (also saved next to the file as "
                                                ".timestamps.txt).")
                elif kind == "finished":
                    msg, err = a
                    self.busy = False
                    self.go_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    if not err:
                        self.overall.set(1)
                    self.toast(msg, error=err)
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _update_ytdlp(self):
        if not messagebox.askyesno(APP_NAME, "Update yt-dlp to the latest version now?\n"
                                   "YouTube changes often; updating fixes most errors.\n\nRestart afterwards."):
            return
        self.toast("Updating yt-dlp…")

        def work():
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"],
                               capture_output=True, text=True, creationflags=core.NO_WINDOW)
            self.post("log", (r.stdout or "")[-1500:] + (r.stderr or "")[-1500:])
            self.post("status", "yt-dlp updated – restart YTScrape" if r.returncode == 0 else "Update failed – see Log")

        threading.Thread(target=work, daemon=True).start()

    def _on_close(self):
        if self.busy and not messagebox.askyesno(APP_NAME, "A download is running. Quit anyway?"):
            return
        self.cancel.set()
        self._save_settings()
        self.thumb_pool.shutdown(wait=False, cancel_futures=True)
        self.inspect_pool.shutdown(wait=False, cancel_futures=True)
        self.details_pool.shutdown(wait=False, cancel_futures=True)
        self.destroy()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
