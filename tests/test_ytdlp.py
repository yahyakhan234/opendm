"""yt-dlp integration tests, using tests/fake_ytdlp.py in place of the real tool."""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from opendm import bundle, ytdlp  # noqa: E402
from opendm.engine import Status  # noqa: E402
from opendm.manager import DownloadManager  # noqa: E402
from opendm.server import ApiServer  # noqa: E402

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_ytdlp.py")
URL = "https://www.youtube.com/watch?v=abc123"


class YtdlpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, "out")
        self.log = os.path.join(self.tmp, "args.log")
        os.environ["FAKE_YTDLP_LOG"] = self.log
        os.environ["FAKE_YTDLP_MODE"] = "ok"
        self.dm = DownloadManager(root=os.path.join(self.tmp, "data"))
        self.dm.settings["ytdlp_path"] = FAKE
        self.dm.apply_settings()
        ytdlp._probe_cache.clear()

    def tearDown(self):
        self.dm.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait(self, dl, statuses=(Status.COMPLETE, Status.ERROR), timeout=30):
        t = time.time()
        while dl.status not in statuses:
            if time.time() - t > timeout:
                self.fail(f"timeout: {dl.status} {dl.error}")
            time.sleep(0.05)

    def last_args(self):
        with open(self.log, encoding="utf-8") as f:
            return json.loads(f.read().splitlines()[-1])

    def test_youtube_links_use_ytdlp(self):
        for u in (URL, "https://youtu.be/abc123", "https://m.youtube.com/shorts/abc123"):
            self.assertTrue(ytdlp.is_ytdlp_url(u), u)
        self.assertFalse(ytdlp.is_ytdlp_url("https://notyoutube.com/watch?v=x"))
        dl = self.dm.add(URL, folder=self.out, quality=720)
        self.assertIsInstance(dl, ytdlp.YtdlpDownload)

    def test_video_download_with_quality(self):
        dl = self.dm.add(URL, folder=self.out, quality=720)
        self.wait(dl)
        self.assertEqual(dl.status, Status.COMPLETE, dl.error)
        self.assertEqual(os.path.basename(dl.final_path), "Fake Video_ Test! (720p).mp4")
        self.assertEqual(dl.progress, 1.0)
        args = self.last_args()
        self.assertIn("bv*[height<=720]+ba", args[args.index("-f") + 1])
        self.assertEqual(args[-2:], ["--", URL], "URL must come after -- so it can't be read as an option")
        self.assertIn("--merge-output-format", args)
        self.assertFalse(os.path.exists(dl.temp_dir))

    def test_browser_title_is_used(self):
        dl = self.dm.add(URL, folder=self.out, filename="My Song", quality="audio")
        self.wait(dl)
        self.assertEqual(os.path.basename(dl.final_path), "My Song.m4a")
        args = self.last_args()
        self.assertTrue(args[args.index("-f") + 1].startswith("bestaudio"))

    def test_drm_error_is_readable(self):
        os.environ["FAKE_YTDLP_MODE"] = "drm"
        dl = self.dm.add(URL, folder=self.out)
        self.wait(dl)
        self.assertEqual(dl.status, Status.ERROR)
        self.assertIn("DRM", dl.error)

    def test_pause_and_resume(self):
        os.environ["FAKE_YTDLP_MODE"] = "slow"
        dl = self.dm.add(URL, folder=self.out, quality=720)
        while dl.downloaded < 4_000_000:
            time.sleep(0.05)
        self.assertGreater(dl.progress, 0)
        self.dm.pause(dl)
        self.wait(dl, (Status.PAUSED,), timeout=10)
        os.environ["FAKE_YTDLP_MODE"] = "ok"
        self.dm.resume(dl)
        self.wait(dl)
        self.assertEqual(dl.status, Status.COMPLETE, dl.error)
        self.assertIn("--continue", self.last_args())

    def test_missing_ytdlp(self):
        self.dm.settings["ytdlp_path"] = ""
        self.dm.apply_settings()
        orig = ytdlp.find_ytdlp
        ytdlp.find_ytdlp = lambda configured=None: None
        try:
            dl = self.dm.add(URL, folder=self.out)
            self.wait(dl)
        finally:
            ytdlp.find_ytdlp = orig
        self.assertIn("winget install yt-dlp", dl.error)

    def test_probe_lists_qualities(self):
        info = ytdlp.probe(URL, FAKE)
        self.assertEqual(info["title"], "Fake Video: Test!")
        self.assertEqual([o["height"] for o in info["options"]], [1080, 720, 360])
        self.assertEqual(info["options"][0]["size"], 53_000_000, "video-only size includes the audio track")
        self.assertEqual(info["options"][1]["fps"], 60)

    def test_without_ffmpeg_only_single_file_qualities(self):
        info = ytdlp.probe(URL, FAKE, can_merge=False)
        self.assertEqual([o["height"] for o in info["options"]], [360])
        self.assertTrue(info["needs_ffmpeg"])
        self.assertEqual(ytdlp.format_for(720, can_merge=False), "b[height<=720]/b")

    def test_youtube_video_without_ffmpeg_explains(self):
        orig = ytdlp.find_ffmpeg
        ytdlp.find_ffmpeg = lambda configured=None: None
        try:
            dl = self.dm.add(URL, folder=self.out, quality=720)
            self.wait(dl)
            self.assertEqual(dl.status, Status.ERROR)
            self.assertIn("ffmpeg", dl.error)
            audio = self.dm.add(URL, folder=self.out, quality="audio")  # audio needs no ffmpeg
            self.wait(audio)
            self.assertEqual(audio.status, Status.COMPLETE, audio.error)
            self.assertNotIn("--merge-output-format", self.last_args())
        finally:
            ytdlp.find_ffmpeg = orig

    def test_bundled_copy_is_installed_and_upgraded(self):
        shipped = os.path.join(self.tmp, "meipass", "tools")
        os.makedirs(shipped)
        data = os.path.join(self.tmp, "appdata")

        def read(path):
            with open(path, "rb") as f:
                return f.read()

        def ship(version, content):
            with open(os.path.join(shipped, "yt-dlp.exe"), "wb") as f:
                f.write(content)
            with open(os.path.join(shipped, "yt-dlp.version"), "w") as f:
                f.write(version)

        old_env = os.environ.get("OPENDM_DATA")
        os.environ["OPENDM_DATA"] = data
        sys._MEIPASS = os.path.dirname(shipped)
        try:
            ship("2026.01.01", b"v1")
            path = ytdlp.find_ytdlp()
            self.assertEqual(path, os.path.join(data, "tools", "yt-dlp.exe"))
            self.assertEqual(read(path), b"v1")
            # yt-dlp updated itself since: an older bundle must not overwrite it.
            with open(path, "wb") as f:
                f.write(b"self-updated")
            bundle.write_state("yt-dlp", {"version": "2026.06.01", "checked": 0})
            ship("2026.03.01", b"v2")
            self.assertEqual(read(ytdlp.find_ytdlp()), b"self-updated")
            # A newer OpenDM.exe ships a newer yt-dlp: that one wins.
            ship("2026.09.01", b"v3")
            self.assertEqual(read(ytdlp.find_ytdlp()), b"v3")
            # A path chosen in Settings always comes first.
            self.assertEqual(ytdlp.find_ytdlp(FAKE), FAKE)
        finally:
            del sys._MEIPASS
            if old_env is None:
                os.environ.pop("OPENDM_DATA", None)
            else:
                os.environ["OPENDM_DATA"] = old_env

    def test_bundled_ffmpeg_zip_is_unpacked(self):
        import zipfile

        from opendm import hls

        bundle_dir = os.path.join(self.tmp, "meipass", "tools")
        os.makedirs(bundle_dir)
        with zipfile.ZipFile(os.path.join(bundle_dir, "ffmpeg.zip"), "w", zipfile.ZIP_LZMA) as z:
            z.writestr("ffmpeg.exe", b"ffmpeg-binary" * 1000)
            z.writestr("LICENSE.txt", "GPL")
        with open(os.path.join(bundle_dir, "ffmpeg.version"), "w") as f:
            f.write("2026.09.30")
        data = os.path.join(self.tmp, "appdata")
        old_env = os.environ.get("OPENDM_DATA")
        os.environ["OPENDM_DATA"] = data
        sys._MEIPASS = os.path.dirname(bundle_dir)
        try:
            path = hls.find_ffmpeg()
            self.assertEqual(path, os.path.join(data, "tools", "ffmpeg.exe"))
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"ffmpeg-binary" * 1000)
            self.assertTrue(os.path.isfile(os.path.join(data, "tools", "ffmpeg-LICENSE.txt")))
            self.assertTrue(bundle.is_managed(path))
            self.assertEqual(hls.find_ffmpeg(FAKE), FAKE, "a path chosen in Settings comes first")
        finally:
            del sys._MEIPASS
            if old_env is None:
                os.environ.pop("OPENDM_DATA", None)
            else:
                os.environ["OPENDM_DATA"] = old_env

    def test_probe_endpoint(self):
        srv = ApiServer(0, lambda p: None, lambda u: ytdlp.probe(u, FAKE))
        srv.start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{srv.httpd.server_address[1]}/probe",
                data=json.dumps({"url": URL}).encode(),
                headers={"X-OpenDM-Client": "t", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.load(r)
            self.assertTrue(body["ok"])
            self.assertEqual(body["options"][0]["height"], 1080)
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()
