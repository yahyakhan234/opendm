"""Tkinter desktop UI for OpenDM."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

from . import APP_NAME, __version__
from .engine import Status
from .hls import find_ffmpeg
from .ytdlp import find_ytdlp, update_managed_ytdlp
from . import bundle
from .ytdlp import probe as ytdlp_probe
from . import tray
from .icon import tray_ico, window_png
from .manager import DownloadManager
from .server import ApiServer
from .util import CATEGORIES, category_for, human_size, human_time, is_url

# ---------------------------------------------------------------- palette
BG = "#14161b"
PANEL = "#1b1e25"
FIELD = "#232731"
HOVER = "#2b303c"
BORDER = "#2e3340"
TEXT = "#e7e9ee"
MUTED = "#8b93a3"
ACCENT = "#3b82f6"
ACCENT_HI = "#5b9bff"
GREEN = "#22c55e"
RED = "#ef4444"
AMBER = "#f59e0b"
FONT = ("Segoe UI", 10)
FONT_SM = ("Segoe UI", 9)
FONT_B = ("Segoe UI Semibold", 10)
FONT_H = ("Segoe UI Semibold", 12)

FILTERS = [
    ("All downloads", lambda d: True),
    ("Downloading", lambda d: d.status in Status.ACTIVE),
    ("Unfinished", lambda d: d.status != Status.COMPLETE),
    ("Completed", lambda d: d.status == Status.COMPLETE),
] + [(cat, (lambda c: lambda d: category_for(d.display_name()) == c)(cat)) for cat in list(CATEGORIES) + ["Other"]]

FILE_EXTS = set().union(*CATEGORIES.values()) - {".svg"}


def open_path(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def reveal_path(path: str) -> None:
    if sys.platform == "win32" and os.path.exists(path):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        open_path(os.path.dirname(path) if os.path.isfile(path) else path)


def text_bar(frac: float | None, width: int = 12) -> str:
    if frac is None:
        return ""
    filled = int(round(frac * width))
    return "█" * filled + "░" * (width - filled) + f"  {frac * 100:5.1f}%"


# ================================================================ styling
def apply_theme(root: tk.Tk) -> None:
    root.configure(bg=BG)
    root.option_add("*Font", FONT)
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=BG, foreground=TEXT, fieldbackground=FIELD, bordercolor=BORDER,
                 lightcolor=BORDER, darkcolor=BORDER, troughcolor=FIELD, focuscolor=ACCENT, font=FONT)
    st.configure("TFrame", background=BG)
    st.configure("Panel.TFrame", background=PANEL)
    st.configure("TLabel", background=BG, foreground=TEXT)
    st.configure("Panel.TLabel", background=PANEL)
    st.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=FONT_SM)
    st.configure("MutedBg.TLabel", background=BG, foreground=MUTED, font=FONT_SM)
    st.configure("Head.TLabel", background=PANEL, font=FONT_H)
    st.configure("Status.TLabel", background=PANEL, foreground=MUTED, font=FONT_SM, padding=(10, 4))

    st.configure("TButton", background=FIELD, foreground=TEXT, padding=(12, 6), borderwidth=0, relief="flat")
    st.map("TButton", background=[("pressed", BORDER), ("active", HOVER), ("disabled", PANEL)],
           foreground=[("disabled", MUTED)])
    st.configure("Accent.TButton", background=ACCENT, foreground="white", font=FONT_B)
    st.map("Accent.TButton", background=[("pressed", ACCENT), ("active", ACCENT_HI)])
    st.configure("Tool.TButton", background=PANEL, padding=(12, 8))
    st.map("Tool.TButton", background=[("pressed", BORDER), ("active", HOVER), ("disabled", PANEL)],
           foreground=[("disabled", "#555c6b")])
    st.configure("ToolAccent.TButton", background=ACCENT, foreground="white", padding=(14, 8), font=FONT_B)
    st.map("ToolAccent.TButton", background=[("pressed", ACCENT), ("active", ACCENT_HI)])

    st.configure("TEntry", fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT, padding=6, borderwidth=1)
    st.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)])
    st.configure("TSpinbox", fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT, arrowcolor=TEXT,
                 background=FIELD, padding=4)
    st.configure("TCheckbutton", background=BG, foreground=TEXT, indicatorbackground=FIELD,
                 indicatorforeground=TEXT)
    st.map("TCheckbutton", background=[("active", BG)], indicatorbackground=[("selected", ACCENT)])

    st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=30,
                 borderwidth=0, font=FONT)
    st.map("Treeview", background=[("selected", "#24324d")], foreground=[("selected", TEXT)])
    st.configure("Treeview.Heading", background=BG, foreground=MUTED, borderwidth=0, padding=(8, 6),
                 font=FONT_SM, relief="flat")
    st.map("Treeview.Heading", background=[("active", PANEL)])
    st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    st.configure("Side.Treeview", background=BG, fieldbackground=BG, rowheight=32)
    st.map("Side.Treeview", background=[("selected", PANEL)], foreground=[("selected", ACCENT_HI)])

    st.configure("TScrollbar", background=PANEL, troughcolor=BG, arrowcolor=MUTED, borderwidth=0)
    st.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD, thickness=6, borderwidth=0)


# ================================================================ dialogs
class Dialog(tk.Toplevel):
    def __init__(self, master, title: str):
        super().__init__(master)
        self.withdraw()
        self.title(title)
        self.configure(bg=BG)
        if master.winfo_viewable():
            self.transient(master)
        self.resizable(False, False)
        self.body = ttk.Frame(self, padding=18)
        self.body.pack(fill="both", expand=True)
        self.bind("<Escape>", lambda e: self.destroy())

    def show(self):
        self.update_idletasks()
        m = self.master
        x = m.winfo_rootx() + (m.winfo_width() - self.winfo_reqwidth()) // 2
        y = m.winfo_rooty() + (m.winfo_height() - self.winfo_reqheight()) // 3
        if not m.winfo_viewable():
            x = (self.winfo_screenwidth() - self.winfo_reqwidth()) // 2
            y = (self.winfo_screenheight() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(300, lambda: self.attributes("-topmost", False))
        self.focus_force()


class AddDialog(Dialog):
    def __init__(self, app: "App", url: str = "", filename: str = "", headers: dict | None = None,
                 size: int | None = None, media: dict | None = None):
        super().__init__(app, "Add video" if (media or {}).get("kind") in ("hls", "ytdlp") else "Add download")
        self.app = app
        self.headers = headers or {}
        self.media = media or {}
        self.media_url = url.strip()
        s = app.manager.settings
        b = self.body
        b.columnconfigure(1, weight=1)

        ttk.Label(b, text="Address", style="MutedBg.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        self.url = tk.Text(b, height=3, width=64, bg=FIELD, fg=TEXT, insertbackground=TEXT, relief="flat",
                           highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
                           font=FONT, wrap="char", padx=6, pady=6)
        self.url.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(2, 2))
        self.url.insert("1.0", url)
        hint = "One URL per line to add several at once. Video stream links (.m3u8) work too."
        if size and size > 0:
            hint = f"Size reported by browser: {human_size(size)}"
        if self.media.get("kind") in ("hls", "ytdlp"):
            q = self.media.get("quality")
            what = "YouTube video" if self.media["kind"] == "ytdlp" else "Video stream"
            if q == "audio":
                hint = f"{what} · audio only · saved as .m4a"
            else:
                hint = f"{what}{f' · {q}p' if q else ''} · saved as .mp4"
        ttk.Label(b, text=hint, style="MutedBg.TLabel").grid(row=2, column=0, columnspan=3, sticky="w",
                                                             pady=(0, 10))

        ttk.Label(b, text="Save as").grid(row=3, column=0, sticky="w", pady=4)
        self.name = ttk.Entry(b)
        self.name.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=4)
        self.name.insert(0, filename)
        ttk.Label(b, text="Leave blank to use the server's filename.", style="MutedBg.TLabel").grid(
            row=4, column=1, columnspan=2, sticky="w", padx=(10, 0))

        ttk.Label(b, text="Folder").grid(row=5, column=0, sticky="w", pady=(10, 4))
        self.folder = ttk.Entry(b)
        self.folder.grid(row=5, column=1, sticky="ew", padx=(10, 6), pady=(10, 4))
        self.folder.insert(0, s["download_dir"])
        ttk.Button(b, text="Browse…", command=self._browse).grid(row=5, column=2, pady=(10, 4))

        row = ttk.Frame(b)
        row.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Label(row, text="Connections").pack(side="left")
        self.conns = tk.IntVar(value=s["connections"])
        ttk.Spinbox(row, from_=1, to=32, textvariable=self.conns, width=5).pack(side="left", padx=(10, 24))
        self.start_now = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="Start immediately", variable=self.start_now).pack(side="left")

        btns = ttk.Frame(b)
        btns.grid(row=7, column=0, columnspan=3, sticky="e", pady=(18, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Download", style="Accent.TButton", command=self._ok).pack(side="right", padx=8)
        self.bind("<Control-Return>", lambda e: self._ok())
        self.show()
        (self.url if not url else self.name).focus_set()
        if url and not filename:
            self.after(10, lambda: self.focus_force())

    def _browse(self):
        d = filedialog.askdirectory(parent=self, initialdir=self.folder.get() or None)
        if d:
            self.folder.delete(0, "end")
            self.folder.insert(0, d)

    def _ok(self):
        urls = [u.strip() for u in self.url.get("1.0", "end").split() if u.strip()]
        bad = [u for u in urls if not is_url(u)]
        if not urls or bad:
            messagebox.showerror(APP_NAME, "Enter a valid http(s) address." + (f"\n\n{bad[0]}" if bad else ""),
                                 parent=self)
            return
        folder = self.folder.get().strip() or None
        if folder == self.app.manager.settings["download_dir"]:
            folder = None  # lets "sort into categories" apply
        name = self.name.get().strip() if len(urls) == 1 else None
        try:
            conns = int(self.conns.get())
        except (tk.TclError, ValueError):
            conns = None
        for u in urls:
            # Stream details from the browser only apply if the address wasn't edited.
            media = self.media if (len(urls) == 1 and u == self.media_url) else {}
            self.app.manager.add(u, folder=folder, filename=name or None, connections=conns,
                                 headers=self.headers, start=self.start_now.get(), **media)
        self.destroy()
        self.app.refresh()


class SettingsDialog(Dialog):
    def __init__(self, app: "App"):
        super().__init__(app, "Settings")
        self.app = app
        s = app.manager.settings
        b = self.body
        b.columnconfigure(1, weight=1)

        ttk.Label(b, text="Default download folder").grid(row=0, column=0, sticky="w", pady=6)
        self.folder = ttk.Entry(b, width=46)
        self.folder.insert(0, s["download_dir"])
        self.folder.grid(row=0, column=1, sticky="ew", padx=10)
        ttk.Button(b, text="Browse…", command=self._browse).grid(row=0, column=2)

        self.vars = {}

        def spin(r, label, key, lo, hi, hint=""):
            ttk.Label(b, text=label).grid(row=r, column=0, sticky="w", pady=6)
            v = tk.IntVar(value=s[key])
            self.vars[key] = v
            f = ttk.Frame(b)
            f.grid(row=r, column=1, columnspan=2, sticky="w", padx=10)
            ttk.Spinbox(f, from_=lo, to=hi, textvariable=v, width=8).pack(side="left")
            if hint:
                ttk.Label(f, text=hint, style="MutedBg.TLabel").pack(side="left", padx=10)

        spin(1, "Connections per download", "connections", 1, 32, "8 is a good default; more can upset servers")
        spin(2, "Downloads at the same time", "max_concurrent", 1, 20)
        spin(3, "Speed limit (KB/s)", "speed_limit_kbps", 0, 10_000_000, "0 = unlimited")
        spin(4, "Browser integration port", "port", 1024, 65535, "restart needed; match the extension")

        def check(r, label, key):
            v = tk.BooleanVar(value=s[key])
            self.vars[key] = v
            ttk.Checkbutton(b, text=label, variable=v).grid(row=r, column=0, columnspan=3, sticky="w", pady=4)

        check(5, "Watch the clipboard for download links", "clipboard_monitor")
        check(6, "Ask before starting downloads sent from the browser", "confirm_browser_downloads")
        check(7, "Sort finished files into category folders (Video, Music, Programs…)", "sort_into_categories")
        check(8, "Keep running in the system tray when closed or minimized", "minimize_to_tray")

        ff = find_ffmpeg(s.get("ffmpeg_path") or None)
        ttk.Label(b, text="ffmpeg (for videos)").grid(row=9, column=0, sticky="w", pady=(10, 4))
        self.ffmpeg = ttk.Entry(b, width=46)
        self.ffmpeg.insert(0, s.get("ffmpeg_path") or "")
        self.ffmpeg.grid(row=9, column=1, sticky="ew", padx=10, pady=(10, 4))
        ttk.Button(b, text="Browse…", command=self._browse_ffmpeg).grid(row=9, column=2, pady=(10, 4))
        if bundle.is_managed(ff):
            msg = "Built in (comes with OpenDM)"
        else:
            msg = (f"Found: {ff}" if ff else
                   "Not found. Videos are saved as .ts; install it with:  winget install ffmpeg")
        ttk.Label(b, text=msg if len(msg) < 90 else "Found: …" + msg[-80:], style="MutedBg.TLabel").grid(
            row=10, column=1, columnspan=2, sticky="w", padx=10)

        yt = find_ytdlp(s.get("ytdlp_path") or None)
        ttk.Label(b, text="yt-dlp (for YouTube)").grid(row=11, column=0, sticky="w", pady=(10, 4))
        self.ytdlp = ttk.Entry(b, width=46)
        self.ytdlp.insert(0, s.get("ytdlp_path") or "")
        self.ytdlp.grid(row=11, column=1, sticky="ew", padx=10, pady=(10, 4))
        ttk.Button(b, text="Browse…", command=self._browse_ytdlp).grid(row=11, column=2, pady=(10, 4))
        if bundle.is_managed(yt):
            msg = "Built in (comes with OpenDM and updates itself daily)"
        else:
            msg = f"Found: {yt}" if yt else "Not found. Install it with:  winget install yt-dlp"
        ttk.Label(b, text=msg if len(msg) < 90 else "Found: …" + msg[-80:], style="MutedBg.TLabel").grid(
            row=12, column=1, columnspan=2, sticky="w", padx=10)

        btns = ttk.Frame(b)
        btns.grid(row=13, column=0, columnspan=3, sticky="e", pady=(18, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Save", style="Accent.TButton", command=self._save).pack(side="right", padx=8)
        self.show()

    def _browse(self):
        d = filedialog.askdirectory(parent=self, initialdir=self.folder.get() or None)
        if d:
            self.folder.delete(0, "end")
            self.folder.insert(0, d)

    def _browse_ytdlp(self):
        f = filedialog.askopenfilename(parent=self, title="Find yt-dlp",
                                       filetypes=[("yt-dlp", "yt-dlp.exe yt-dlp"), ("All files", "*")])
        if f:
            self.ytdlp.delete(0, "end")
            self.ytdlp.insert(0, f)

    def _browse_ffmpeg(self):
        f = filedialog.askopenfilename(parent=self, title="Find ffmpeg",
                                       filetypes=[("ffmpeg", "ffmpeg.exe ffmpeg"), ("All files", "*")])
        if f:
            self.ffmpeg.delete(0, "end")
            self.ffmpeg.insert(0, f)

    def _save(self):
        s = self.app.manager.settings
        try:
            for k, v in self.vars.items():
                s[k] = v.get()
        except (tk.TclError, ValueError):
            messagebox.showerror(APP_NAME, "Please enter whole numbers.", parent=self)
            return
        folder = self.folder.get().strip()
        if folder:
            s["download_dir"] = folder
        s["ffmpeg_path"] = self.ffmpeg.get().strip()
        s["ytdlp_path"] = self.ytdlp.get().strip()
        self.app.manager.apply_settings()
        self.destroy()


class Toast(tk.Toplevel):
    """Small 'download complete' popup in the bottom-right corner."""

    def __init__(self, app: "App", dl):
        super().__init__(app)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=BORDER)
        inner = tk.Frame(self, bg=PANEL, padx=16, pady=12)
        inner.pack(padx=1, pady=1)
        ok = dl.status == Status.COMPLETE
        tk.Label(inner, text="Download complete" if ok else "Download failed", bg=PANEL,
                 fg=GREEN if ok else RED, font=FONT_B).pack(anchor="w")
        tk.Label(inner, text=dl.display_name()[:48], bg=PANEL, fg=TEXT, font=FONT).pack(anchor="w", pady=(2, 8))
        if not ok:
            tk.Label(inner, text=(dl.error or "")[:60], bg=PANEL, fg=MUTED, font=FONT_SM).pack(anchor="w")
        row = tk.Frame(inner, bg=PANEL)
        row.pack(anchor="e", fill="x")
        if ok and dl.final_path:
            ttk.Button(row, text="Open", style="Accent.TButton",
                       command=lambda: (open_path(dl.final_path), self.destroy())).pack(side="left")
            ttk.Button(row, text="Show in folder",
                       command=lambda: (reveal_path(dl.final_path), self.destroy())).pack(side="left", padx=6)
        ttk.Button(row, text="Close", command=self.destroy).pack(side="right")
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        offset = 60 + 8 * (len(app.toasts) % 5)
        self.geometry(f"+{self.winfo_screenwidth() - w - 24}+{self.winfo_screenheight() - h - offset}")
        app.toasts.append(self)
        self.bind("<Destroy>", lambda e: app.toasts.remove(self) if e.widget is self and self in app.toasts else None)
        self.after(8000, self._close)

    def _close(self):
        if self.winfo_exists():
            self.destroy()


# ================================================================ main window
class App(tk.Tk):
    COLUMNS = (
        ("name", "File name", 320, "w"),
        ("size", "Size", 90, "e"),
        ("progress", "Progress", 190, "w"),
        ("speed", "Speed", 100, "e"),
        ("eta", "Time left", 90, "e"),
        ("status", "Status", 110, "w"),
    )

    def __init__(self, manager: DownloadManager, initial_urls: list[str] | None = None):
        super().__init__()
        self.manager = manager
        self.events: queue.Queue = queue.Queue()
        self.toasts: list[Toast] = []
        self.filter_idx = 0
        self.sort_key = None
        self.sort_rev = False
        self._last_clip = ""
        self._row_cache: dict[str, tuple] = {}

        self.title(APP_NAME)
        self.geometry("1100x680")
        self.minsize(820, 480)
        try:
            self._icon = tk.PhotoImage(data=window_png())
            self.iconphoto(True, self._icon)
        except tk.TclError:
            pass
        apply_theme(self)
        self._build()

        manager.listeners.append(lambda ev, dl: self.events.put(("dl", ev, dl)))
        self.server = None
        try:
            self.server = ApiServer(int(manager.settings["port"]), lambda p: self.events.put(("api", p)),
                                    self._probe)
            self.server.start()
        except OSError:
            self.status_msg.set(f"Browser integration off: port {manager.settings['port']} is busy")

        self.tray = None
        self.tray_events: queue.Queue = queue.Queue()
        if tray.AVAILABLE:
            try:
                ico = os.path.join(manager.root, "opendm.ico")
                with open(ico, "wb") as f:
                    f.write(tray_ico())
                self.tray = tray.TrayIcon(ico, APP_NAME, self.tray_events.put)
                if not self.tray.start():
                    self.tray = None
            except OSError:
                self.tray = None
        self.bind("<Unmap>", self._on_unmap)
        # The yt-dlp that ships with OpenDM.exe updates itself once a day; YouTube breaks old versions.
        threading.Thread(target=self._prepare_tools, name="tools", daemon=True).start()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Control-n>", lambda e: self.add_dialog())
        self.bind("<Delete>", lambda e: self.delete_selected())
        self.bind("<Control-v>", self._paste)
        self.bind("<Control-a>", lambda e: self.tree.selection_set(self.tree.get_children()))
        try:
            self._last_clip = self.clipboard_get()
        except tk.TclError:
            pass
        self.refresh()
        self._poll()
        self._tray_poll()
        self._clipboard_loop()
        if initial_urls:
            self.after(200, lambda: AddDialog(self, "\n".join(initial_urls)))

    # ------------------------------------------------------------ layout
    def _build(self):
        bar = ttk.Frame(self, style="Panel.TFrame", padding=(10, 8))
        bar.pack(fill="x")
        ttk.Button(bar, text="＋  Add URL", style="ToolAccent.TButton", command=self.add_dialog).pack(side="left")
        ttk.Frame(bar, width=10, style="Panel.TFrame").pack(side="left")
        self.btn_resume = ttk.Button(bar, text="▶  Resume", style="Tool.TButton", command=self.resume_selected)
        self.btn_pause = ttk.Button(bar, text="❚❚  Pause", style="Tool.TButton", command=self.pause_selected)
        self.btn_delete = ttk.Button(bar, text="✕  Delete", style="Tool.TButton", command=self.delete_selected)
        self.btn_folder = ttk.Button(bar, text="🗀  Open folder", style="Tool.TButton", command=self.open_folder)
        for w in (self.btn_resume, self.btn_pause, self.btn_delete, self.btn_folder):
            w.pack(side="left", padx=2)
        ttk.Button(bar, text="⚙  Settings", style="Tool.TButton",
                   command=lambda: SettingsDialog(self)).pack(side="right", padx=2)
        ttk.Button(bar, text="Pause all", style="Tool.TButton", command=self.manager.pause_all).pack(side="right", padx=2)
        ttk.Button(bar, text="Resume all", style="Tool.TButton", command=self.manager.resume_all).pack(side="right", padx=2)

        status = ttk.Frame(self, style="Panel.TFrame")
        status.pack(side="bottom", fill="x")
        self.status_msg = tk.StringVar(value=f"{APP_NAME} {__version__}")
        self.status_speed = tk.StringVar()
        ttk.Label(status, textvariable=self.status_msg, style="Status.TLabel").pack(side="left")
        ttk.Label(status, textvariable=self.status_speed, style="Status.TLabel").pack(side="right")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        # Sidebar filters
        side = ttk.Frame(body, padding=(8, 10, 0, 10))
        side.pack(side="left", fill="y")
        self.side = ttk.Treeview(side, style="Side.Treeview", show="tree", selectmode="browse", height=12)
        self.side.column("#0", width=170)
        for i, (name, _) in enumerate(FILTERS):
            label = ("   " if i >= 4 else "") + name
            self.side.insert("", "end", iid=str(i), text=label)
            if i == 3:
                self.side.insert("", "end", iid="sep", text="  CATEGORIES", tags=("sep",))
        self.side.tag_configure("sep", foreground=MUTED, font=FONT_SM)
        self.side.selection_set("0")
        self.side.bind("<<TreeviewSelect>>", self._on_filter)
        self.side.pack(fill="y", expand=True)

        main = ttk.Frame(body, padding=(8, 10, 10, 10))
        main.pack(side="left", fill="both", expand=True)

        # Download list
        listf = ttk.Frame(main)
        listf.pack(fill="both", expand=True)
        cols = [c[0] for c in self.COLUMNS]
        self.tree = ttk.Treeview(listf, columns=cols, show="headings", selectmode="extended")
        for key, title, width, anchor in self.COLUMNS:
            self.tree.heading(key, text=title, anchor=anchor, command=lambda k=key: self._sort(k))
            self.tree.column(key, width=width, anchor=anchor, stretch=key == "name")
        sb = ttk.Scrollbar(listf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("done", foreground=GREEN)
        self.tree.tag_configure("error", foreground=RED)
        self.tree.tag_configure("paused", foreground=MUTED)
        self.tree.tag_configure("active", foreground=TEXT)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_details())
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<Button-3>", self._on_right_click)

        self.empty = tk.Label(listf, text="No downloads yet\n\nClick “＋ Add URL”, press Ctrl+V with a link copied,\n"
                                          "or install the browser extension to catch downloads automatically.",
                              bg=PANEL, fg=MUTED, font=FONT, justify="center")

        # Details panel
        det = ttk.Frame(main, style="Panel.TFrame", padding=14)
        det.pack(fill="x", pady=(10, 0))
        det.columnconfigure(1, weight=1)
        self.d_name = tk.StringVar()
        self.d_info = tk.StringVar()
        self.d_url = tk.StringVar()
        ttk.Label(det, textvariable=self.d_name, style="Head.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(det, textvariable=self.d_info, style="Muted.TLabel").grid(row=1, column=0, columnspan=2,
                                                                            sticky="w", pady=(2, 0))
        ttk.Label(det, textvariable=self.d_url, style="Muted.TLabel").grid(row=2, column=0, columnspan=2,
                                                                           sticky="w", pady=(0, 8))
        self.seg_canvas = tk.Canvas(det, height=26, bg=FIELD, highlightthickness=0)
        self.seg_canvas.grid(row=3, column=0, columnspan=2, sticky="ew")
        self.seg_canvas.bind("<Configure>", lambda e: self._update_details())
        ttk.Label(det, text="Each colored block is one connection's piece of the file.",
                  style="Muted.TLabel").grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

        self.menu = tk.Menu(self, tearoff=0, bg=PANEL, fg=TEXT, activebackground=ACCENT, activeforeground="white",
                            bd=0, relief="flat")
        self.menu.add_command(label="Open file", command=self.open_file)
        self.menu.add_command(label="Show in folder", command=self.open_folder)
        self.menu.add_separator()
        self.menu.add_command(label="Resume", command=self.resume_selected)
        self.menu.add_command(label="Pause", command=self.pause_selected)
        self.menu.add_command(label="Download again", command=self.redownload_selected)
        self.menu.add_separator()
        self.menu.add_command(label="Copy address", command=self.copy_url)
        self.menu.add_command(label="Open address in browser",
                              command=lambda: [webbrowser.open(d.url) for d in self.selected()[:1]])
        self.menu.add_separator()
        self.menu.add_command(label="Remove from list", command=self.delete_selected)
        self.menu.add_command(label="Delete file too", command=lambda: self.delete_selected(with_file=True))

    # --------------------------------------------------------- selection
    def selected(self):
        ids = set(self.tree.selection())
        return [d for d in self.manager.downloads if d.id in ids]

    def _on_filter(self, _e=None):
        sel = self.side.selection()
        if not sel:
            return
        if sel[0] == "sep":
            self.side.selection_set(str(self.filter_idx))
            return
        self.filter_idx = int(sel[0])
        self.refresh()

    def _sort(self, key):
        self.sort_rev = (not self.sort_rev) if self.sort_key == key else False
        self.sort_key = key
        self.refresh()

    def _on_double(self, e):
        row = self.tree.identify_row(e.y)
        d = next((x for x in self.manager.downloads if x.id == row), None)
        if not d:
            return
        if d.status == Status.COMPLETE:
            self.open_file()
        elif d.status in (Status.PAUSED, Status.ERROR):
            self.manager.resume(d)
        else:
            self.manager.pause(d)

    def _on_right_click(self, e):
        row = self.tree.identify_row(e.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
        if self.tree.selection():
            self.menu.tk_popup(e.x_root, e.y_root)

    # ----------------------------------------------------------- actions
    def add_dialog(self, url: str = ""):
        if not url:
            try:
                clip = self.clipboard_get().strip()
                url = clip if is_url(clip) else ""
            except tk.TclError:
                pass
        AddDialog(self, url)

    def _paste(self, _e=None):
        if isinstance(self.focus_get(), (tk.Entry, ttk.Entry, tk.Text)):
            return
        self.add_dialog()

    def resume_selected(self):
        for d in self.selected():
            self.manager.resume(d)

    def pause_selected(self):
        for d in self.selected():
            self.manager.pause(d)

    def redownload_selected(self):
        for d in self.selected():
            self.manager.redownload(d)

    def delete_selected(self, with_file: bool = False):
        sel = self.selected()
        if not sel:
            return
        what = f"{len(sel)} downloads" if len(sel) > 1 else f"“{sel[0].display_name()}”"
        msg = f"Delete {what} and the downloaded file{'s' if len(sel) > 1 else ''} from disk?" if with_file \
            else f"Remove {what} from the list?\n\nFinished files stay on disk; partial downloads are discarded."
        if not messagebox.askyesno(APP_NAME, msg, parent=self):
            return
        for d in sel:
            self.manager.remove(d, delete_file=with_file)
        self.refresh()

    def open_file(self):
        for d in self.selected()[:1]:
            if d.final_path and os.path.exists(d.final_path):
                open_path(d.final_path)
            else:
                messagebox.showinfo(APP_NAME, "The file isn't available (not finished, moved or deleted).", parent=self)

    def open_folder(self):
        sel = self.selected()
        if sel and sel[0].final_path and os.path.exists(sel[0].final_path):
            reveal_path(sel[0].final_path)
        else:
            folder = sel[0].folder if sel else self.manager.settings["download_dir"]
            os.makedirs(folder, exist_ok=True)
            open_path(folder)

    def copy_url(self):
        sel = self.selected()
        if sel:
            self.clipboard_clear()
            self.clipboard_append("\n".join(d.url for d in sel))
            self._last_clip = "\n".join(d.url for d in sel)

    # ------------------------------------------------------------ events
    def _poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "dl" and ev[1] == "finished":
                    dl = ev[2]
                    if dl.status == Status.COMPLETE:
                        self.status_msg.set(f"Finished: {dl.display_name()}")
                        Toast(self, dl)
                    elif dl.status == Status.ERROR:
                        self.status_msg.set(f"Failed: {dl.display_name()} — {dl.error}")
                        Toast(self, dl)
                elif ev[0] == "api":
                    self._from_browser(ev[1])
        except queue.Empty:
            pass
        self.refresh()
        self._update_tray_tip()
        self.after(500, self._poll)

    def _from_browser(self, p: dict):
        if p.get("action") == "show":
            self.show_window()
            return
        headers = {}
        if p.get("referrer"):
            headers["Referer"] = p["referrer"]
        if p.get("cookies"):
            headers["Cookie"] = p["cookies"]
        if p.get("useragent"):
            headers["User-Agent"] = p["useragent"]
        urls = p.get("urls") or [p["url"]]
        # Video streams picked from the "Download this video" button.
        media = {k: p.get(k) for k in ("kind", "audio_url", "quality") if p.get(k)}
        if self.manager.settings.get("confirm_browser_downloads", True):
            if self.state() == "iconic":
                self.deiconify()
            AddDialog(self, "\n".join(urls), p.get("filename") or "", headers, p.get("size"), media)
        else:
            for u in urls:
                self.manager.add(u, filename=(p.get("filename") or None) if len(urls) == 1 else None,
                                 headers=headers, **(media if len(urls) == 1 else {}))
            self.status_msg.set(f"Added {len(urls)} download(s) from the browser")

    def _clipboard_loop(self):
        if self.manager.settings.get("clipboard_monitor"):
            try:
                clip = self.clipboard_get().strip()
            except tk.TclError:
                clip = ""
            if clip and clip != self._last_clip:
                self._last_clip = clip
                if is_url(clip):
                    path = clip.split("?")[0].split("#")[0].lower()
                    if os.path.splitext(path)[1] in FILE_EXTS:
                        AddDialog(self, clip)
        self.after(1000, self._clipboard_loop)

    # ----------------------------------------------------------- refresh
    def _row(self, d):
        prog = d.progress
        size = human_size(d.total) if d.total is not None else ""
        if size and d.size_estimated:
            size = "~" + size
        if d.status == Status.DOWNLOADING:
            speed = f"{human_size(d.speed)}/s" if d.speed else ""
            eta = human_time(d.eta)
            if prog is None:
                size = f"{human_size(d.downloaded)}…"
        else:
            speed = eta = ""
        status = d.status
        if d.status == Status.DOWNLOADING:
            status = f"Downloading ×{d.active_connections}"
        tag = {"Complete": "done", "Error": "error", "Paused": "paused", "Queued": "paused"}.get(d.status, "active")
        return (d.display_name(), size, text_bar(prog), speed, eta, status), tag

    def refresh(self):
        _, fn = FILTERS[self.filter_idx]
        items = [d for d in self.manager.downloads if fn(d)]
        if self.sort_key:
            keyfuncs = {
                "name": lambda d: d.display_name().lower(),
                "size": lambda d: d.total or 0,
                "progress": lambda d: d.progress or 0,
                "speed": lambda d: d.speed,
                "eta": lambda d: d.eta or 0,
                "status": lambda d: d.status,
            }
            items.sort(key=keyfuncs[self.sort_key], reverse=self.sort_rev)
        else:
            items.reverse()  # newest first

        wanted = [d.id for d in items]
        existing = set(self.tree.get_children())
        for iid in existing - set(wanted):
            self.tree.delete(iid)
            self._row_cache.pop(iid, None)
        for i, d in enumerate(items):
            values, tag = self._row(d)
            if d.id not in existing:
                self.tree.insert("", i, iid=d.id, values=values, tags=(tag,))
            else:
                if self._row_cache.get(d.id) != (values, tag):
                    self.tree.item(d.id, values=values, tags=(tag,))
                if self.tree.index(d.id) != i:
                    self.tree.move(d.id, "", i)
            self._row_cache[d.id] = (values, tag)

        if items:
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.45, anchor="center")

        active = [d for d in self.manager.downloads if d.status in Status.ACTIVE]
        queued = sum(1 for d in self.manager.downloads if d.status == Status.QUEUED)
        speed = self.manager.total_speed()
        parts = []
        if active:
            parts.append(f"{len(active)} active")
        if queued:
            parts.append(f"{queued} queued")
        if speed:
            parts.append(f"↓ {human_size(speed)}/s")
        lim = self.manager.settings.get("speed_limit_kbps")
        if lim:
            parts.append(f"limit {human_size(lim * 1024)}/s")
        self.status_speed.set("   ·   ".join(parts))
        self.title(f"{APP_NAME} — ↓ {human_size(speed)}/s" if speed else APP_NAME)

        sel = self.selected()
        can_resume = any(d.status in (Status.PAUSED, Status.ERROR) for d in sel)
        can_pause = any(d.status in Status.ACTIVE or d.status == Status.QUEUED for d in sel)
        self.btn_resume.state(["!disabled"] if can_resume else ["disabled"])
        self.btn_pause.state(["!disabled"] if can_pause else ["disabled"])
        self.btn_delete.state(["!disabled"] if sel else ["disabled"])
        self._update_details()

    def _update_details(self):
        sel = self.selected()
        c = self.seg_canvas
        c.delete("all")
        if not sel:
            self.d_name.set("No download selected")
            self.d_info.set("Select a download to see its details.")
            self.d_url.set("")
            return
        d = sel[0]
        self.d_name.set(d.display_name())
        bits = [d.status if d.status != Status.ERROR else f"Error: {d.error}"]
        if d.kind == "ytdlp":
            label = f"YouTube via yt-dlp · {d.label()}"
            if d.status == Status.DOWNLOADING and d.quality != "audio":
                label += " · audio" if d.stage else " · video"
            bits.append(label)
        if d.kind == "hls":
            label = "Video stream" + (f" · {d.variant_label}" if d.variant_label else "")
            if d.units and d.status != Status.COMPLETE:
                label += f" · {d.done_units}/{d.units} chunks"
            bits.append(label)
        if d.total is not None:
            approx = "~" if d.size_estimated else ""
            bits.append(f"{human_size(d.downloaded)} of {approx}{human_size(d.total)}")
        else:
            bits.append(human_size(d.downloaded))
        if d.status == Status.DOWNLOADING:
            bits.append(f"{human_size(d.speed)}/s")
            bits.append(f"{d.active_connections} connections")
            if d.eta:
                bits.append(f"{human_time(d.eta)} left")
        if d.total is not None and d.status != Status.COMPLETE and d.kind != "hls":
            bits.append("resumable" if d.resumable else "not resumable (server doesn't support it)")
        if d.final_path:
            bits.append(d.final_path)
        if getattr(d, "note", None):
            bits.append(d.note.strip())
        self.d_info.set("   ·   ".join(bits))
        self.d_url.set(d.url if len(d.url) < 140 else d.url[:137] + "…")

        w = c.winfo_width()
        h = 26
        if d.status == Status.COMPLETE:
            c.create_rectangle(0, 0, w, h, fill=GREEN, width=0)
            return
        total, segs = d.bar()
        if not total or not segs:
            frac = d.progress or 0
            c.create_rectangle(0, 0, w * frac, h, fill=ACCENT, width=0)
            return
        palette = [ACCENT, "#6366f1", "#06b6d4", "#8b5cf6", "#0ea5e9", "#14b8a6", "#a855f7", "#3b82f6"]
        for i, (start, end, pos) in enumerate(sorted(segs)):
            x0 = start / total * w
            x1 = (end + 1) / total * w if end is not None else w
            xp = pos / total * w
            col = palette[i % len(palette)]
            if xp > x0:
                c.create_rectangle(x0, 0, xp, h, fill=col, width=0)
            if pos <= (end if end is not None else pos):
                c.create_line(xp, 0, xp, h, fill="white", width=1)
            c.create_line(x1, 0, x1, h, fill=BG, width=1)

    # ------------------------------------------------------------- close
    def on_close(self):
        if self._tray_enabled():
            self.hide_to_tray()
        else:
            self.quit_app()

    @staticmethod
    def _prepare_tools():
        # Unpack the bundled ffmpeg now (it takes a few seconds the first time) rather than
        # when the first video is requested, then let yt-dlp check for an update.
        bundle.install("ffmpeg")
        update_managed_ytdlp()

    def _probe(self, url: str) -> dict:
        s = self.manager.settings
        can_merge = bool(find_ffmpeg(s.get("ffmpeg_path") or None))
        return ytdlp_probe(url, find_ytdlp(s.get("ytdlp_path") or None), can_merge)

    def _tray_enabled(self) -> bool:
        return self.tray is not None and bool(self.manager.settings.get("minimize_to_tray", True))

    def hide_to_tray(self):
        self.withdraw()
        if not self.manager.settings.get("tray_hint_shown"):
            self.manager.settings["tray_hint_shown"] = True
            self.manager.apply_settings()
            self.tray.balloon(APP_NAME, "OpenDM is still running here, and downloads keep going. "
                                        "Right-click the icon to exit.")

    def show_window(self):
        self.deiconify()
        self.state("normal")
        self.lift()
        self.attributes("-topmost", True)
        self.after(200, lambda: self.attributes("-topmost", False))
        self.focus_force()

    def _on_unmap(self, e):
        # Minimize goes to the tray, like IDM.
        if e.widget is self and self._tray_enabled() and self.state() == "iconic":
            self.after(10, self.withdraw)

    def _tray_poll(self):
        try:
            while True:
                cmd = self.tray_events.get_nowait()
                if cmd == "open":
                    self.show_window()
                elif cmd == "add":
                    self.show_window()
                    self.add_dialog()
                elif cmd == "resume_all":
                    self.manager.resume_all()
                elif cmd == "pause_all":
                    self.manager.pause_all()
                elif cmd == "exit":
                    self.quit_app()
                    return
        except queue.Empty:
            pass
        self.after(100, self._tray_poll)

    def _update_tray_tip(self):
        if not self.tray:
            return
        active = [d for d in self.manager.downloads if d.status in Status.ACTIVE]
        if active:
            speed = sum(d.speed for d in active)
            tip = f"{APP_NAME} - {len(active)} downloading, {human_size(speed)}/s"
        else:
            tip = f"{APP_NAME} - no active downloads"
        self.tray.set_tooltip(tip)

    def quit_app(self):
        if any(d.status in Status.ACTIVE for d in self.manager.downloads):
            self.show_window()
            if not messagebox.askyesno(APP_NAME, "Downloads are in progress.\n\nQuit anyway? "
                                                 "They'll pick up where they left off next time you open "
                                                 f"{APP_NAME}.", parent=self):
                return
        self.status_msg.set("Saving…")
        self.update_idletasks()
        if self.tray:
            self.tray.stop()
        if self.server:
            self.server.stop()
        self.manager.shutdown()
        self.destroy()


def run(initial_urls: list[str] | None = None, manager: DownloadManager | None = None,
        start_hidden: bool = False) -> None:
    mutex = None
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("OpenDM.App")
            # Lets the installer see that OpenDM is running and ask to close it before updating.
            mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "OpenDM-Running")
        except Exception:
            pass
    app = App(manager or DownloadManager(), initial_urls)
    if start_hidden and app.tray is not None:
        app.withdraw()
    app.mainloop()
    del mutex  # released when the process exits
