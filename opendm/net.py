"""Fast connections when a server has several addresses ("Happy Eyeballs", RFC 8305).

Python's socket.create_connection tries a host's addresses one after another and waits for
each to time out. On networks where IPv6 is advertised but doesn't actually work (common),
that means ~20 s of "Connecting" before it falls back to IPv4. Browsers instead start the
next address after a short delay and keep whichever connects first. install() makes every
connection in OpenDM (urllib, http.client) do the same.
"""

from __future__ import annotations

import queue
import socket
import threading

STAGGER = 0.25  # seconds to wait for one address before also trying the next (RFC 8305)

_original = socket.create_connection
_installed = False


def _interleave(infos: list) -> list:
    """Alternate address families, starting with the resolver's first choice."""
    first = [i for i in infos if i[0] == infos[0][0]]
    other = [i for i in infos if i[0] != infos[0][0]]
    out = []
    for a, b in zip(first, other):
        out += [a, b]
    longer = first if len(first) > len(other) else other
    return out + longer[min(len(first), len(other)):]


def create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *args, **kwargs):
    host, port = address[0], address[1]
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except OSError:
        return _original(address, timeout, source_address, *args, **kwargs)
    if len(infos) < 2:
        return _original(address, timeout, source_address, *args, **kwargs)

    results: queue.Queue = queue.Queue()
    winner = threading.Event()

    def attempt(info):
        family, socktype, proto, _canon, addr = info
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(addr)
            if winner.is_set():  # another address already won
                sock.close()
                return
            results.put(("ok", sock))
        except OSError as e:
            if sock is not None:
                sock.close()
            results.put(("err", e))

    pending = 0
    last_error: OSError | None = None
    for info in _interleave(infos):
        threading.Thread(target=attempt, args=(info,), daemon=True).start()
        pending += 1
        try:
            kind, value = results.get(timeout=STAGGER)
        except queue.Empty:
            continue
        pending -= 1
        if kind == "ok":
            winner.set()
            _drain(results, pending)
            return value
        last_error = value  # this one failed fast: try the next straight away
    while pending:
        wait = None if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout
        try:
            kind, value = results.get(timeout=wait)
        except queue.Empty:
            break
        pending -= 1
        if kind == "ok":
            winner.set()
            _drain(results, pending)
            return value
        last_error = value
    winner.set()
    raise last_error or OSError(f"could not connect to {host}:{port}")


def _drain(results: queue.Queue, pending: int) -> None:
    """Close sockets from attempts that connected at the same moment as the winner."""
    def close_late():
        for _ in range(pending):
            try:
                kind, value = results.get(timeout=60)
            except queue.Empty:
                return
            if kind == "ok":
                value.close()
    if pending:
        threading.Thread(target=close_late, daemon=True).start()


def install() -> None:
    global _installed
    if not _installed:
        socket.create_connection = create_connection
        _installed = True
