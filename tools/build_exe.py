"""Build a standalone OpenDM.exe (no Python needed to run it).

    python -m venv .venv
    .venv\Scripts\pip install pyinstaller
    .venv\Scripts\python tools/build_exe.py

Writes dist/OpenDM.exe. ffmpeg and yt-dlp are not bundled; OpenDM finds them if
they are installed (winget install Gyan.FFmpeg / winget install yt-dlp).
"""

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
sys.path.insert(0, ROOT)

from opendm import __version__  # noqa: E402
from opendm.icon import ico_bytes  # noqa: E402


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


def main() -> None:
    os.makedirs(BUILD, exist_ok=True)
    ico = os.path.join(BUILD, "opendm.ico")
    with open(ico, "wb") as f:
        f.write(ico_bytes())
    ver = os.path.join(BUILD, "version.txt")
    version_file(ver)
    subprocess.check_call([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile", "--windowed", "--name", "OpenDM",
        "--icon", ico, "--version-file", ver,
        "--distpath", os.path.join(ROOT, "dist"),
        "--workpath", os.path.join(BUILD, "pyinstaller"),
        "--specpath", BUILD,
        os.path.join(ROOT, "OpenDM.pyw"),
    ], cwd=ROOT)
    shutil.rmtree(os.path.join(BUILD, "pyinstaller"), ignore_errors=True)
    print("built", os.path.join(ROOT, "dist", "OpenDM.exe"))


if __name__ == "__main__":
    main()
