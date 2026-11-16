"""Build the browser-extension zips, one per store.

  dist/opendm-firefox-<version>.zip  -> addons.mozilla.org (AMO signs it for you)
  dist/opendm-chrome-<version>.zip   -> Chrome Web Store / Edge Add-ons

The extension/ folder itself stays loadable unpacked in both browsers. The store
builds differ in two ways:
  * Each manifest only has the background key its browser uses, so neither store
    warns about the other browser's key.
  * The Chrome build leaves out YouTube downloading (the button is hidden on
    YouTube and the menu explains why), because Chrome Web Store policy rejects
    YouTube downloaders. Firefox keeps it.

Usage:  python tools/package_extension.py
"""

import copy
import json
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = os.path.join(ROOT, "extension")
DIST = os.path.join(ROOT, "dist")

YOUTUBE_MATCHES = [
    "*://*.youtube.com/*",
    "*://youtube.com/*",
    "*://*.youtube-nocookie.com/*",
    "*://youtu.be/*",
]


def firefox_manifest(m: dict) -> dict:
    m = copy.deepcopy(m)
    m["background"] = {"scripts": m["background"]["scripts"]}
    return m


def chrome_manifest(m: dict) -> dict:
    m = copy.deepcopy(m)
    m["background"] = {"service_worker": m["background"]["service_worker"]}
    m.pop("browser_specific_settings", None)
    for cs in m["content_scripts"]:
        cs["exclude_matches"] = YOUTUBE_MATCHES
    return m


def chrome_background(src: str) -> str:
    flag = "const YOUTUBE_SUPPORT = true;"
    if flag not in src:
        raise SystemExit("background.js no longer contains the YOUTUBE_SUPPORT flag")
    return src.replace(flag, "const YOUTUBE_SUPPORT = false;", 1)


def build(name: str, manifest: dict, patch: dict) -> str:
    out = os.path.join(DIST, f"opendm-{name}-{manifest['version']}.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        for folder, _, files in os.walk(EXT):
            for fname in sorted(files):
                path = os.path.join(folder, fname)
                # Files must sit at the zip root, with forward-slash paths.
                arc = os.path.relpath(path, EXT).replace(os.sep, "/")
                if arc == "manifest.json":
                    continue
                if arc in patch:
                    with open(path, encoding="utf-8") as f:
                        z.writestr(arc, patch[arc](f.read()))
                else:
                    z.write(path, arc)
    return out


def main() -> None:
    with open(os.path.join(EXT, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    os.makedirs(DIST, exist_ok=True)
    print("wrote", build("firefox", firefox_manifest(manifest), {}))
    print("wrote", build("chrome", chrome_manifest(manifest), {"background.js": chrome_background}))


if __name__ == "__main__":
    main()
