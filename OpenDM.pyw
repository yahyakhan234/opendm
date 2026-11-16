# Double-click this file on Windows to start OpenDM without a console window.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from opendm.__main__ import main  # noqa: E402

sys.exit(main(sys.argv[1:]))
