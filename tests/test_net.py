"""Connections must not hang on an address that doesn't answer (e.g. broken IPv6)."""

import http.server
import os
import socket
import sys
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from opendm import net  # noqa: E402

BLACKHOLE = "10.255.255.1"  # private, unrouted: connecting just hangs


class NetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        net.install()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def resolve_to(self, *ips):
        real = socket.getaddrinfo

        def fake(host, port, *a, **k):
            if host == "dual.test":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
            return real(host, port, *a, **k)

        socket.getaddrinfo = fake
        self.addCleanup(setattr, socket, "getaddrinfo", real)

    def test_dead_first_address_does_not_stall(self):
        self.resolve_to(BLACKHOLE, "127.0.0.1")
        t = time.time()
        with urllib.request.urlopen(f"http://dual.test:{self.port}/", timeout=30) as r:
            self.assertEqual(r.read(), b"ok")
        self.assertLess(time.time() - t, 3, "should move on to the working address quickly")

    def test_refused_first_address_falls_through(self):
        self.resolve_to("127.0.0.2", "127.0.0.1")  # nothing listens on 127.0.0.2: refused at once
        sock = socket.create_connection(("dual.test", self.port), timeout=10)
        self.assertEqual(sock.getpeername()[0], "127.0.0.1")
        sock.close()

    def test_all_dead_raises(self):
        self.resolve_to(BLACKHOLE, "10.255.255.2")
        with self.assertRaises(OSError):
            socket.create_connection(("dual.test", 80), timeout=1)

    def test_interleave_alternates_families(self):
        a6 = [(socket.AF_INET6, 1, 6, "", ("::1", 1, 0, 0))] * 3
        a4 = [(socket.AF_INET, 1, 6, "", ("1.1.1.1", 1))] * 2
        fams = [i[0] for i in net._interleave(a6 + a4)]
        self.assertEqual(fams, [socket.AF_INET6, socket.AF_INET, socket.AF_INET6, socket.AF_INET, socket.AF_INET6])


if __name__ == "__main__":
    unittest.main()
