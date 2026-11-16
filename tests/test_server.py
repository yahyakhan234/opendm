import json
import os
import sys
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from opendm.server import ApiServer  # noqa: E402


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.received = []
        cls.srv = ApiServer(0, cls.received.append)
        cls.port = cls.srv.httpd.server_address[1]
        cls.srv.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def setUp(self):
        self.received.clear()

    def call(self, path, body=None, headers=None, method=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers or {},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_ping_requires_client_header(self):
        self.assertEqual(self.call("/ping")[0], 403)
        self.assertEqual(self.call("/ping", headers={"X-OpenDM-Client": "x"})[0], 200)

    def test_websites_are_rejected(self):
        h = {"X-OpenDM-Client": "x", "Origin": "https://evil.example", "Content-Type": "application/json"}
        self.assertEqual(self.call("/add", {"url": "https://a.com/f.zip"}, h)[0], 403)
        self.assertEqual(self.call("/add", method="OPTIONS")[0], 403)
        self.assertEqual(self.received, [])

    def test_extension_can_add_batch(self):
        h = {"X-OpenDM-Client": "x", "Origin": "chrome-extension://abc", "Content-Type": "application/json"}
        code, body = self.call("/add", {"urls": ["https://a.com/1.zip", "https://a.com/2.zip", "javascript:x"]}, h)
        self.assertEqual(code, 200)
        self.assertEqual(body["count"], 2)
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0]["urls"], ["https://a.com/1.zip", "https://a.com/2.zip"])

    def test_bad_payload(self):
        h = {"X-OpenDM-Client": "x", "Content-Type": "application/json"}
        self.assertEqual(self.call("/add", {"url": "not a url"}, h)[0], 400)


if __name__ == "__main__":
    unittest.main()
