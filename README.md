# OpenDM

A free, open-source download manager for Windows (and anywhere Python runs).
It speeds up downloads by using several connections at once, resumes after a pause,
a crash or a reboot, and catches downloads straight from your browser.

**No dependencies.** Pure Python standard library + Tkinter, so it runs anywhere Python 3.10+ does.

## Features

- **Multi-connection downloads.** Each file is split into up to 32 pieces that download in parallel.
- **Dynamic segmentation.** When a connection finishes its piece, it takes over half of the
  slowest remaining piece, so every connection stays busy until the end.
- **Pause / resume.** Works across app restarts and reboots (on servers that support byte ranges).
  Downloads that were running when you closed the app resume automatically.
- **Automatic retries.** Dropped connections retry with backoff without losing progress.
- **Queue.** Choose how many downloads run at once; the rest wait their turn.
- **Speed limiter.** A global cap shared by all downloads.
- **Browser integration.** One extension for Firefox and Chrome (plus Edge, Brave, Opera, Vivaldi)
  sends downloads to OpenDM, with the page's cookies and referrer so logged-in downloads keep working.
  If OpenDM isn't open, the browser downloads normally.
- **"Download this video".** Hover a video and a button appears on the player. Pick a quality
  (1080p, 720p…) and OpenDM downloads it over several connections, decrypting standard
  AES-128 streams and merging video + audio into one `.mp4`. Works with HLS (`.m3u8`) streams and
  plain video files; you can also paste an `.m3u8` link into **Add URL**.
- **YouTube** (through [yt-dlp](https://github.com/yt-dlp/yt-dlp), built into the exe). The same button lists YouTube's
  real qualities with sizes, plus **Audio only**. You can also paste a YouTube link into **Add URL**.
- **Right-click menu.** "Download with OpenDM" on any link, image or video, plus
  "Download all links on this page".
- **Clipboard monitor (optional).** Copy a link to a file and OpenDM offers to download it.
- **Categories.** Filter by Video, Music, Programs and more; optionally sort finished files into subfolders.
- **Segment view.** See each connection's progress, like IDM's block view.
- **System tray.** Closing or minimizing the window keeps OpenDM running in the tray, like IDM; downloads carry on. Click the icon to bring it back, or right-click for Add URL, Resume all, Pause all and Exit. You can turn this off in Settings.
- **Single instance.** Running `opendm <url>` while the app is open hands the URL to the open window.

## Install & run

### Easiest: the installer

Download **`OpenDM-Setup-<version>.exe`** from the [latest release](https://github.com/yahyakhan234/opendm/releases/latest)
and run it. It installs for your user only (no admin prompt), adds a Start-menu shortcut, can start
OpenDM with Windows (it waits quietly in the tray), and has a normal uninstaller. Nothing else to
install: Python, yt-dlp and ffmpeg are all included. The installed version starts in about a second.

### Portable: the single exe

Prefer not to install? Download **`OpenDM.exe`** and double-click it. Same app in one file.
Settings, the download list and the unpacked tools are kept in `%LOCALAPPDATA%\OpenDM`, so you can
move the exe anywhere.

The exe isn't code-signed, so the first time Windows may show "Windows protected your PC".
Click **More info** → **Run anyway**. The portable exe takes about 2 seconds to start because it
unpacks itself first, and its very first launch also unpacks yt-dlp and ffmpeg (about 190 MB) into
the data folder.

### From source

You need [Python 3.10+](https://www.python.org/downloads/) (check "Add to PATH" and keep "tcl/tk" ticked).

```bash
git clone https://github.com/yahyakhan234/opendm.git
cd opendm
```

Then either double-click **`OpenDM.pyw`** (no console window), or run:

```bash
python -m opendm
```

Download something from the command line (opens the app, or adds to the running one):

```bash
python -m opendm https://example.com/big-file.zip
```

## Browser extension (Firefox & Chrome)

The `extension` folder works in both Firefox (128+) and Chromium browsers (Chrome, Edge, Brave, Opera, Vivaldi).
Keep OpenDM running and downloads you start in the browser go to OpenDM. If the app is closed,
the browser downloads the file itself as usual.

### Chrome / Edge / Brave

1. Open `chrome://extensions` (or `edge://extensions`, `brave://extensions`).
2. Turn on **Developer mode**.
3. Click **Load unpacked** and pick the `extension` folder.

### Firefox

For a quick try (removed when Firefox restarts):

1. Open `about:debugging#/runtime/this-firefox`.
2. Click **Load Temporary Add-on…** and pick `extension/manifest.json`.

For a permanent install, Firefox only accepts signed add-ons. Build the zip (below) and upload it to
[addons.mozilla.org](https://addons.mozilla.org/developers/). Choose "On your own" to get a signed `.xpi`
without a public listing, or list it publicly.

If Firefox shows a yellow **Grant access** box in the OpenDM popup, click it. Firefox lets you
install extensions without site access, and OpenDM needs that access to talk to the app and
pass along cookies for logged-in downloads.

### Packaging for the stores

```bash
python tools/package_extension.py
```

This writes two zips to `dist/`:

- `opendm-firefox-<version>.zip` for addons.mozilla.org.
- `opendm-chrome-<version>.zip` for the Chrome Web Store and Edge Add-ons. This build leaves out
  YouTube downloading, because Chrome Web Store policy rejects YouTube downloaders. Chrome users can
  still paste a YouTube link into the app's Add URL.

### Building the exe and the installer

```bash
python -m venv .venv
.venv\Scripts\pip install pyinstaller
.venv\Scripts\python tools/build_exe.py
.venv\Scripts\python tools/build_exe.py --installer
```

The first writes the portable `dist/OpenDM.exe`. The second writes `dist/OpenDM-Setup-<version>.exe`
and needs [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`winget install JRSoftware.InnoSetup`);
the installer script is `installer/opendm.iss`.

Both include yt-dlp and ffmpeg, taken from the copies
installed on your PC. `winget install yt-dlp` installs both (yt-dlp's own ffmpeg build is preferred).
Use `--ytdlp PATH` / `--ffmpeg PATH` to pick other copies, or `--no-ytdlp` / `--no-ffmpeg` to leave them out.

### Downloading videos

Play a video, hover it, and click **Download this video**. Then pick a quality.
The extension icon shows how many videos it found on the page, and the popup lists them too.

- **ffmpeg** turns streams into normal `.mp4` files and merges separate audio tracks. It's built into
  `OpenDM.exe`. Running from source, install it with `winget install ffmpeg`; OpenDM finds it
  automatically, or you can set its location in Settings. Without it, streams are saved as `.ts`,
  which VLC and most players handle.
- **YouTube** uses yt-dlp and ffmpeg (YouTube sends video and sound as separate streams, and ffmpeg
  joins them). `OpenDM.exe` comes with both and lets yt-dlp update itself once a day, since YouTube
  changes often and old versions stop working. Running from source, install them with
  `winget install yt-dlp ffmpeg`; without ffmpeg only **Audio only** is offered.
  OpenDM runs yt-dlp in the background, showing progress, pause and resume in the
  OpenDM window. Files are H.264 + AAC `.mp4` up to 1080p (VP9/AV1 above that, which is all YouTube
  offers there), or `.m4a` for audio only. If you installed yt-dlp yourself and downloads start
  failing, run `winget upgrade yt-dlp`. Downloading from YouTube is against YouTube's terms of service, so it's
  your call what you save.
- **Not supported:** DRM-protected video (Netflix, Disney+, Prime Video, Spotify and similar) is
  shown greyed out and is never downloaded. Live streams aren't supported.
- Only download videos you have the right to save.

### Using the extension

Click the extension icon to turn catching on or off, skip file types, ignore small files,
or grab every file link on the current page. Right-click any link for **Download with OpenDM**.
Downloads from private/incognito windows are always left to the browser.

## Keyboard shortcuts

| Key | Action |
| --- | --- |
| `Ctrl+N` | Add a download |
| `Ctrl+V` | Add the link on your clipboard |
| `Delete` | Remove selected downloads |
| `Ctrl+A` | Select all |
| Double-click | Open a finished file / pause or resume |

## How it works

```
probe  ──►  Range: bytes=0-0  →  size, real filename, range support, final URL
split  ──►  N segments, each downloaded by its own connection into its own temp file
steal  ──►  a finished connection splits the biggest remaining segment in half
merge  ──►  segments are joined in order into the final file, temp files deleted
```

Each segment gets its own temp file instead of everything writing into one preallocated file.
That avoids Windows zero-filling multi-gigabyte files, and resuming is just
"append to the segment file from its current size".

Settings, the download list and temp segments live in `%LOCALAPPDATA%\OpenDM`.
Set the `OPENDM_DATA` environment variable to keep them somewhere else (handy for a portable USB copy).

### Project layout

```
opendm/
  engine.py    segmented download engine (probe, workers, retries, work stealing, merge)
  hls.py       video stream (HLS/.m3u8) downloader: quality choice, chunks, AES-128, ffmpeg merge
  aes.py       AES-128 decryption (Windows crypto API, pure-Python fallback)
  ytdlp.py     YouTube via yt-dlp: quality lookup, progress parsing, pause/resume
  bundle.py    unpacks the yt-dlp and ffmpeg that ship inside OpenDM.exe
  manager.py   queue, settings, persistence, speed limiter
  server.py    localhost API used by the browser extension (127.0.0.1:6801)
  gui.py       Tkinter interface
  tray.py      system tray icon and menu (plain Windows API via ctypes)
  icon.py      app icon, drawn in code (python -m opendm.icon regenerates extension icons)
extension/     browser extension for Firefox + Chromium (one Manifest V3 codebase)
               background.js: download catching + video detection; content.js: the video button
tools/         package_extension.py builds the store-ready zips; build_exe.py builds OpenDM.exe
tests/         end-to-end tests against a local range-capable HTTP server
```

### Security note

The local API only listens on `127.0.0.1` and requires a custom `X-OpenDM-Client` header.
Websites can't send that header to another origin without a CORS preflight, which OpenDM
always refuses. Requests carrying a website `Origin` are rejected too, so a web page can't
queue downloads behind your back.

## Running the tests

```bash
python -m unittest discover -s tests -v
```

## Limitations / ideas for contributors

- No system tray icon yet (Tkinter has none built in).
- Video: no DASH (`.mpd`) streams or live streams yet. Other yt-dlp sites could be enabled
  the same way YouTube is. No scheduler yet.
- FTP works single-connection only.

PRs welcome!

## License

OpenDM is [MIT](LICENSE) licensed.

`OpenDM.exe` also contains two separate programs that OpenDM runs, each under its own license:

- [yt-dlp](https://github.com/yt-dlp/yt-dlp): the Unlicense (public domain).
- [FFmpeg](https://ffmpeg.org): GPL, from the [yt-dlp FFmpeg builds](https://github.com/yt-dlp/FFmpeg-Builds),
  unmodified. Its license and where to get its source are unpacked next to it as
  `%LOCALAPPDATA%\OpenDM\tools\ffmpeg-LICENSE.txt` and `ffmpeg-SOURCE.txt`.
