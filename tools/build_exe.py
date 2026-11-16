"""Build a standalone OpenDM.exe (no Python needed to run it).

    python -m venv .venv
    .venv\Scripts\pip install pyinstaller
    .venv\Scripts\python tools/build_exe.py

Writes dist/OpenDM.exe with yt-dlp packed inside, so YouTube works out of the box.
The yt-dlp.exe installed on this PC is used (winget install yt-dlp), or pass
--ytdlp PATH; --no-ytdlp builds without it. At runtime OpenDM copies it to its data
folder and lets it update itself daily.

ffmpeg is not bundled (it's large); OpenDM uses it if installed (winget install ffmpeg).
"""

import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
sys.path.insert(0, ROOT)

from opendm import __version__  # noqa: E402
from opendm.icon import ico_bytes  # noqa: E402
from opendm.ytdlp import find_ytdlp  # noqa: E402


def version_file(path: str) -> None:
    """Windows file properties (shown in Explorer > Properties > Details)."""
    nums = tuple(int(x) for x in __version__.split(".")) + (0,) * (4 - len(__version__.split(".")))
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'OpenDM contributors'),
      StringStruct('FileDescription', 'OpenDM download manager'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', 'OpenDM'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', 'OpenDM.exe'),
      StringStruct('ProductName', 'OpenDM'),
      StringStruct('ProductVersion', '{__version__}')])]),
        VarFileInfo([VarStruct('Translation', [1033, 1200])])])
""")


def stage_ytdlp(path: str | None) -> list[str]:
    """Copy yt-dlp.exe and its version into build/tools; return the PyInstaller arguments."""
    exe = path or find_ytdlp()
    if not exe or not exe.lower().endswith("yt-dlp.exe") or not os.path.isfile(exe):
        raise SystemExit("yt-dlp.exe not found. Install it (winget install yt-dlp), "
                         "pass --ytdlp PATH, or build without it using --no-ytdlp.")
    version = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
    if not version:
        raise SystemExit(f"{exe} didn't report a version")
    tools = os.path.join(BUILD, "tools")
    os.makedirs(tools, exist_ok=True)
    shutil.copyfile(exe, os.path.join(tools, "yt-dlp.exe"))
    with open(os.path.join(tools, "yt-dlp.version"), "w", encoding="utf-8") as f:
        f.write(version)
    print(f"bundling yt-dlp {version} from {exe}")
    return ["--add-binary", f"{os.path.join(tools, 'yt-dlp.exe')}{os.pathsep}tools",
            "--add-data", f"{os.path.join(tools, 'yt-dlp.version')}{os.pathsep}tools"]


def main() -> None:
    ap = argparse.ArgumentParser(description="Build dist/OpenDM.exe")
    ap.add_argument("--ytdlp", help="yt-dlp.exe to bundle (default: the installed one)")
    ap.add_argument("--no-ytdlp", action="store_true", help="don't bundle yt-dlp")
    opts = ap.parse_args()
    os.makedirs(BUILD, exist_ok=True)
    extra = [] if opts.no_ytdlp else stage_ytdlp(opts.ytdlp)
    ico = os.path.join(BUILD, "opendm.ico")
    with open(ico, "wb") as f:
        f.write(ico_bytes())
    ver = os.path.join(BUILD, "version.txt")
    version_file(ver)
    subprocess.check_call([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile", "--windowed", "--name", "OpenDM",
        "--icon", ico, "--version-file", ver, *extra,
        "--distpath", os.path.join(ROOT, "dist"),
        "--workpath", os.path.join(BUILD, "pyinstaller"),
        "--specpath", BUILD,
        os.path.join(ROOT, "OpenDM.pyw"),
    ], cwd=ROOT)
    shutil.rmtree(os.path.join(BUILD, "pyinstaller"), ignore_errors=True)
    print("built", os.path.join(ROOT, "dist", "OpenDM.exe"))


if __name__ == "__main__":
    main()
