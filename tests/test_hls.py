"""HLS stream tests against a local server.

The chunks are random bytes, so ffmpeg is switched off here and the expected output is
simply all chunks joined in order. (Real remuxing is checked by hand with a real stream.)
"""

import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from opendm import hls  # noqa: E402
from opendm.aes import py_encrypt  # noqa: E402
from opendm.engine import Status  # noqa: E402
from opendm.manager import DownloadManager  # noqa: E402

KEY = os.urandom(16)
CHUNKS = {name: [os.urandom(n) for _ in range(count)] for name, count, n in (
    ("v720", 40, 50_000), ("v360", 12, 20_000), ("aud", 40, 8_000), ("enc", 12, 16_000))}
# Byte-range stream: one file holding all chunks, plus an init section ("map").
BR_INIT = os.urandom(700)
BR_PARTS = [os.urandom(30_000 + i) for i in range(15)]
BR_FILE = BR_INIT + b"".join(BR_PARTS)


def media_playlist(name, key=False, end=True):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:4", "#EXT-X-MEDIA-SEQUENCE:7"]
    for i, _ in enumerate(CHUNKS[name]):
        if key and i == 0:
            lines.append('#EXT-X-KEY:METHOD=AES-128,URI="/key.bin"')  # IV = sequence number
        if key and i == 6:
            lines.append('#EXT-X-KEY:METHOD=AES-128,URI="/key.bin",IV=0x000102030405060708090a0b0c0d0e0f')
        lines += ["#EXTINF:4.0,", f"{name}/{i}.ts"]
    if end:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines)


def enc_chunk(i):
    iv = (7 + i).to_bytes(16, "big") if i < 6 else bytes(range(16))
    return py_encrypt(KEY, iv, CHUNKS["enc"][i])


ENC_BODIES = {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    slow = False

    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/octet-stream", code=200, extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.slow:
            time.sleep(0.05)
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        m3u = "application/vnd.apple.mpegurl"
        if p == "/master.m3u8":
            return self.send("\n".join([
                "#EXTM3U",
                '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",DEFAULT=YES,URI="/aud.m3u8"',
                '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.4d401e,mp4a.40.2"',
                "/v360.m3u8",
                '#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2"',
                "/v720.m3u8",
            ]), m3u)
        if p == "/master-split.m3u8":
            return self.send("\n".join([
                "#EXTM3U",
                '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a1",NAME="English",DEFAULT=YES,URI="/aud.m3u8"',
                '#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720,AUDIO="a1"',
                "/v720.m3u8",
            ]), m3u)
        if p in ("/v720.m3u8", "/v360.m3u8", "/aud.m3u8"):
            return self.send(media_playlist(p[1:-5]), m3u)
        if p == "/enc.m3u8":
            return self.send(media_playlist("enc", key=True), m3u)
        if p == "/live.m3u8":
            return self.send(media_playlist("v360", end=False), m3u)
        if p == "/drm.m3u8":
            return self.send("#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI=\"skd://abc\",KEYFORMAT=\"com.apple.streamingkeydelivery\"\n"
                             "#EXTINF:4,\nv360/0.ts\n#EXT-X-ENDLIST", m3u)
        if p == "/br.m3u8":
            lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:4", "#EXT-X-PLAYLIST-TYPE:VOD",
                     f'#EXT-X-MAP:URI="/br.mp4",BYTERANGE="{len(BR_INIT)}@0"']
            off = len(BR_INIT)
            for i, part in enumerate(BR_PARTS):
                lines.append("#EXTINF:4.0,")
                lines.append(f"#EXT-X-BYTERANGE:{len(part)}@{off}" if i == 0 else f"#EXT-X-BYTERANGE:{len(part)}")
                lines.append("/br.mp4")
                off += len(part)
            return self.send("\n".join(lines), m3u)
        if p == "/br.mp4":
            rng = self.headers.get("Range", "")
            a, b = rng.split("=")[1].split("-")
            a, b = int(a), int(b)
            return self.send(BR_FILE[a : b + 1], code=206, extra={"Content-Range": f"bytes {a}-{b}/{len(BR_FILE)}"})
        if p == "/key.bin":
            return self.send(KEY)
        name, _, idx = p[1:].partition("/")
        if name in CHUNKS:
            i = int(idx.split(".")[0])
            return self.send(enc_chunk(i) if name == "enc" else CHUNKS[name][i], "video/mp2t")
        self.send("nope", code=404)


def joined(name):
    return b"".join(CHUNKS[name])


class HlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls._orig = hls.find_ffmpeg
        hls.find_ffmpeg = lambda configured=None: None  # byte-exact output, see module docstring

    @classmethod
    def tearDownClass(cls):
        hls.find_ffmpeg = cls._orig
        cls.server.shutdown()

    def setUp(self):
        Handler.slow = False
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, "out")
        self.dm = DownloadManager(root=os.path.join(self.tmp, "data"))

    def tearDown(self):
        self.dm.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait(self, dl, timeout=60):
        t = time.time()
        while dl.status not in (Status.COMPLETE, Status.ERROR):
            if time.time() - t > timeout:
                self.fail(f"timeout: {dl.status} {dl.error}")
            time.sleep(0.05)

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()

    def test_master_picks_best_quality(self):
        dl = self.dm.add(self.base + "/master.m3u8", folder=self.out, filename="My Video")
        self.assertIsInstance(dl, hls.HlsDownload)
        self.wait(dl)
        self.assertEqual(dl.status, Status.COMPLETE, dl.error)
        self.assertEqual(dl.variant_label, "720p")
        self.assertEqual(os.path.basename(dl.final_path), "My Video (720p).ts")
        self.assertEqual(self.read(dl.final_path), joined("v720"))
        self.assertFalse(os.path.exists(dl.temp_dir))

    def test_quality_preference(self):
        dl = self.dm.add(self.base + "/master.m3u8", folder=self.out, quality=480)
        self.wait(dl)
        self.assertEqual(dl.variant_label, "360p")
        self.assertEqual(self.read(dl.final_path), joined("v360"))

    def test_aes128_decryption(self):
        dl = self.dm.add(self.base + "/enc.m3u8", folder=self.out)
        self.wait(dl)
        self.assertEqual(dl.status, Status.COMPLETE, dl.error)
        self.assertEqual(self.read(dl.final_path), joined("enc"))

    def test_separate_audio_without_ffmpeg_gives_two_files(self):
        dl = self.dm.add(self.base + "/master-split.m3u8", folder=self.out, filename="clip")
        self.wait(dl)
        self.assertEqual(dl.status, Status.COMPLETE, dl.error)
        self.assertEqual(self.read(dl.final_path), joined("v720"))
        audio = os.path.join(self.out, "clip (720p) (audio).ts")
        self.assertEqual(self.read(audio), joined("aud"))
        self.assertIn("ffmpeg", dl.note)

    def test_byterange_and_init_map(self):
        dl = self.dm.add(self.base + "/br.m3u8", folder=self.out)
        self.wait(dl)
        self.assertEqual(dl.status, Status.COMPLETE, dl.error)
        self.assertTrue(dl.final_path.endswith(".mp4"))
        self.assertEqual(self.read(dl.final_path), BR_FILE)

    def test_drm_is_refused(self):
        dl = self.dm.add(self.base + "/drm.m3u8", folder=self.out)
        self.wait(dl)
        self.assertEqual(dl.status, Status.ERROR)
        self.assertIn("DRM", dl.error)

    def test_live_is_refused(self):
        dl = self.dm.add(self.base + "/live.m3u8", folder=self.out)
        self.wait(dl)
        self.assertEqual(dl.status, Status.ERROR)
        self.assertIn("live", dl.error)

    def test_pause_resume_across_restart(self):
        Handler.slow = True
        dl = self.dm.add(self.base + "/v720.m3u8", folder=self.out, connections=2)
        while dl.done_units < 6:
            time.sleep(0.02)
        self.dm.pause(dl)
        while dl.status != Status.PAUSED:
            time.sleep(0.02)
        done = dl.done_units
        self.dm.save()
        self.dm.shutdown()

        self.dm = DownloadManager(root=os.path.join(self.tmp, "data"))
        dl2 = self.dm.downloads[0]
        self.assertIsInstance(dl2, hls.HlsDownload)
        self.assertGreaterEqual(dl2.done_units, done)
        self.assertGreater(dl2.progress, 0)
        Handler.slow = False
        self.dm.resume(dl2)
        self.wait(dl2)
        self.assertEqual(dl2.status, Status.COMPLETE, dl2.error)
        self.assertEqual(hashlib.sha256(self.read(dl2.final_path)).digest(),
                         hashlib.sha256(joined("v720")).digest())


if __name__ == "__main__":
    unittest.main()
