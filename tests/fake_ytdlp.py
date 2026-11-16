"""A stand-in for yt-dlp used by the tests. Behaviour is chosen with FAKE_YTDLP_MODE."""
import json
import os
import sys
import time

args = sys.argv[1:]
mode = os.environ.get("FAKE_YTDLP_MODE", "ok")
log = os.environ.get("FAKE_YTDLP_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(args) + "\n")

if mode == "drm":
    print("ERROR: [youtube] abc123: This video is DRM protected", file=sys.stderr)
    sys.exit(1)

if "-J" in args:
    fmts = [
        {"format_id": "140", "vcodec": "none", "acodec": "mp4a", "filesize": 3_000_000},
        {"format_id": "137", "height": 1080, "fps": 30, "vcodec": "avc1", "acodec": "none", "filesize": 50_000_000},
        {"format_id": "136", "height": 720, "fps": 30, "vcodec": "avc1", "acodec": "none", "filesize": 20_000_000},
        {"format_id": "298", "height": 720, "fps": 60, "vcodec": "avc1", "acodec": "none", "filesize": 30_000_000},
        {"format_id": "18", "height": 360, "fps": 30, "vcodec": "avc1", "acodec": "mp4a", "filesize_approx": 8_000_000},
    ]
    print(json.dumps({"title": "Fake Video: Test!", "duration": 120, "is_live": False, "formats": fmts}))
    sys.exit(0)

out_tmpl = args[args.index("-o") + 1]
fmt = args[args.index("-f") + 1]
audio_only = fmt.startswith("bestaudio")
title = "Fake Video: Test!"
print("OPENDMTITLE " + title, flush=True)
delay = 0.25 if mode == "slow" else 0.01
stages = [("140", 3_000_000)] if audio_only else [("136", 20_000_000), ("140", 3_000_000)]
for fid, size in stages:
    for i in range(1, 11):
        print(f"OPENDM {fid} {size * i // 10} {size} NA", flush=True)
        time.sleep(delay)
if not audio_only:
    print("[Merger] Merging formats into \"x.mp4\"", flush=True)
ext = "m4a" if audio_only else "mp4"
path = out_tmpl.replace("%(title).150B", "Fake Video_ Test!").replace("%(id)s", "abc123").replace("%(ext)s", ext)
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "wb") as f:
    f.write(b"fake media " * 1000)
print("OPENDMFILE " + path, flush=True)
