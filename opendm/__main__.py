"""Entry point:  python -m opendm [URL ...]

If OpenDM is already running, the URLs are handed to that window instead of
opening a second copy.
"""

import argparse
import sys

from . import APP_NAME, __version__


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="opendm", description=f"{APP_NAME} download manager")
    parser.add_argument("urls", nargs="*", help="URLs to download")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--tray", action="store_true",
                        help="start hidden in the system tray (used when starting with Windows)")
    args = parser.parse_args(argv)

    import json
    import os

    from .server import send_to_running_instance
    from .util import data_dir

    port = 6801
    try:
        with open(os.path.join(data_dir(), "settings.json"), encoding="utf-8") as f:
            port = int(json.load(f).get("port", port))
    except (OSError, ValueError, TypeError):
        pass
    # Check for a running copy *before* loading state, so two instances never
    # download the same queue or overwrite each other's saved state.
    if args.tray and not args.urls:
        from .server import instance_running

        if instance_running(port):
            return 0  # already running: don't pop the window up at login
    if send_to_running_instance(port, args.urls):
        return 0

    from .gui import run

    run(args.urls, start_hidden=args.tray)
    return 0


if __name__ == "__main__":
    sys.exit(main())
