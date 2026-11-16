"""End-to-end tests against a local HTTP server that supports (and can misbehave with) byte ranges.

Run:  python -m unittest discover -s tests -v
"""

import hashlib
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from opendm.engine import Status  # noqa: E402
from opendm.manager import DownloadManager  # noqa: E402

DATA = os.urandom(12 * 1024 * 1024 + 12345)
SHA = hashlib.sha256(DATA).hexdigest()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    flaky = False      # drop some connections midway
    slow = False       # throttle so we can pause mid-download
    slow_first = False # throttle only the connection that starts at byte 0
    ranges = True
    requests = 0
    block_chrome = False  # hang up on clients calling themselves Chrome (like some bot filters)
    agents: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).requests += 1
        ua = self.headers.get("User-Agent", "")
        type(self).agents.append(ua)
        if self.block_chrome and "Chrome" in ua:
            self.close_connection = True
            self.connection.shutdown(2)  # no response at all
            return
        start, end = 0, len(DATA) - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d+)-(\d*)", rng or "")
        if m and self.ranges:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else end
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(DATA)}")
        else:
            self.send_response(200)
        body = DATA[start : end + 1]
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", 'attachment; filename="test file.bin"')
        self.end_headers()
        sent = 0
        step = 32 * 1024
        try:
            while sent < len(body):
                if self.flaky and sent > 600_000 and type(self).requests % 3 == 0:
                    self.connection.close()  # simulate a dropped connection
                    return
                self.wfile.write(body[sent : sent + step])
                sent += step
                if self.slow or (self.slow_first and start == 0):
                    time.sleep(0.01)
        except (ConnectionError, OSError):
            pass


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/files/test%20file.bin"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Handler.flaky = Handler.slow = Handler.slow_first = Handler.block_chrome = False
        Handler.ranges = True
        Handler.agents = []
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, "out")
        self.dm = DownloadManager(root=os.path.join(self.tmp, "data"))

    def tearDown(self):
        self.dm.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait(self, dl, statuses, timeout=60):
        t = time.time()
        while dl.status not in statuses:
            if time.time() - t > timeout:
                self.fail(f"timeout, status={dl.status} error={dl.error}")
            time.sleep(0.05)

    def check_file(self, dl):
        self.assertEqual(dl.status, Status.COMPLETE, dl.error)
        with open(dl.final_path, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), SHA)

    def test_multi_connection(self):
        dl = self.dm.add(self.url, folder=self.out, connections=8)
        self.wait(dl, {Status.COMPLETE, Status.ERROR})
        self.check_file(dl)
        self.assertEqual(dl.filename, "test file.bin")
        self.assertTrue(dl.resumable)
        self.assertFalse(os.path.exists(dl.temp_dir), "temp segments should be cleaned up")

    def test_retries_under_own_name_when_refused(self):
        Handler.block_chrome = True
        dl = self.dm.add(self.url, folder=self.out, connections=4)
        self.wait(dl, {Status.COMPLETE, Status.ERROR})
        self.check_file(dl)
        self.assertTrue(Handler.agents[0].startswith("Mozilla/"), "browser-like name is tried first")
        self.assertTrue(all(a.startswith("OpenDM/") for a in Handler.agents[1:]),
                        "every later connection uses the name that worked")

    def test_browser_supplied_name_is_kept(self):
        Handler.block_chrome = True
        ua = "Mozilla/5.0 Chrome/131 (from the browser)"
        dl = self.dm.add(self.url, folder=self.out, headers={"User-Agent": ua})
        self.wait(dl, {Status.COMPLETE, Status.ERROR})
        self.assertEqual(dl.status, Status.ERROR)
        self.assertEqual(set(Handler.agents), {ua})

    def test_dynamic_segmentation_splits(self):
        Handler.slow_first = True
        dl = self.dm.add(self.url, folder=self.out, connections=4)
        self.wait(dl, {Status.COMPLETE, Status.ERROR})
        self.check_file(dl)
        self.assertGreater(len(dl.segments), 4, "finished connections should steal work")

    def test_pause_resume_and_restart_app(self):
        Handler.slow = True
        dl = self.dm.add(self.url, folder=self.out, connections=4)
        while dl.downloaded < 2 * 1024 * 1024:
            time.sleep(0.05)
        self.dm.pause(dl)
        self.wait(dl, {Status.PAUSED})
        done = dl.downloaded
        self.assertGreater(done, 0)
        self.dm.save()

        # Simulate closing and reopening the app.
        self.dm.shutdown()
        self.dm = DownloadManager(root=os.path.join(self.tmp, "data"))
        dl2 = self.dm.downloads[0]
        self.assertEqual(dl2.status, Status.PAUSED)
        self.assertEqual(dl2.downloaded, done, "progress should survive a restart")
        Handler.slow = False
        before = Handler.requests
        self.dm.resume(dl2)
        self.wait(dl2, {Status.COMPLETE, Status.ERROR})
        self.check_file(dl2)
        self.assertGreater(Handler.requests, before)

    def test_flaky_connections_retry(self):
        Handler.flaky = True
        dl = self.dm.add(self.url, folder=self.out, connections=6)
        self.wait(dl, {Status.COMPLETE, Status.ERROR}, timeout=120)
        self.check_file(dl)

    def test_server_without_ranges(self):
        Handler.ranges = False
        dl = self.dm.add(self.url, folder=self.out, connections=8)
        self.wait(dl, {Status.COMPLETE, Status.ERROR})
        self.check_file(dl)
        self.assertFalse(dl.resumable)

    def test_speed_limit(self):
        self.dm.settings["speed_limit_kbps"] = 4096  # 4 MB/s
        self.dm.apply_settings()
        t = time.time()
        dl = self.dm.add(self.url, folder=self.out)
        self.wait(dl, {Status.COMPLETE, Status.ERROR})
        self.check_file(dl)
        self.assertGreater(time.time() - t, 2.0, "12 MB at 4 MB/s should take ~3s")

    def test_duplicate_names_not_overwritten(self):
        a = self.dm.add(self.url, folder=self.out)
        self.wait(a, {Status.COMPLETE, Status.ERROR})
        b = self.dm.add(self.url, folder=self.out)
        self.wait(b, {Status.COMPLETE, Status.ERROR})
        self.check_file(b)
        self.assertNotEqual(a.final_path, b.final_path)

    def test_queue_respects_max_concurrent(self):
        Handler.slow = True
        self.dm.settings["max_concurrent"] = 2
        dls = [self.dm.add(self.url, folder=self.out, connections=2) for _ in range(4)]
        time.sleep(0.5)
        self.assertEqual(sum(d.is_running() for d in dls), 2)
        Handler.slow = False
        for d in dls:
            self.wait(d, {Status.COMPLETE, Status.ERROR}, timeout=120)
            self.check_file(d)


if __name__ == "__main__":
    unittest.main()
