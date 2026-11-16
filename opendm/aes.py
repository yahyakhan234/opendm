"""AES-128-CBC decryption for HLS streams that use ``#EXT-X-KEY:METHOD=AES-128``.

This is the plain "clear key" transport encryption from the HLS spec, where the
player downloads the key openly. It is *not* DRM (Widevine/FairPlay/PlayReady);
OpenDM refuses DRM-protected streams instead of trying to get around them.

On Windows it uses the OS crypto library (bcrypt.dll) through ctypes, which is fast.
Elsewhere it falls back to a small pure-Python AES (slow, but no dependencies).
"""

from __future__ import annotations

import sys

# ----------------------------------------------------------------- pure Python AES
_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16"
)
_INV_SBOX = bytearray(256)
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_INV_SBOX = bytes(_INV_SBOX)


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _mul(a: int, b: int) -> int:
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r


_M9 = bytes(_mul(i, 9) for i in range(256))
_M11 = bytes(_mul(i, 11) for i in range(256))
_M13 = bytes(_mul(i, 13) for i in range(256))
_M14 = bytes(_mul(i, 14) for i in range(256))
_M2 = bytes(_mul(i, 2) for i in range(256))
_M3 = bytes(_mul(i, 3) for i in range(256))


def _expand_key(key: bytes) -> list[list[int]]:
    w = [list(key[i : i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= rcon
            rcon = _xtime(rcon)
        w.append([a ^ b for a, b in zip(w[i - 4], t)])
    return [sum(w[r * 4 : r * 4 + 4], []) for r in range(11)]


def _encrypt_block(rk, block: bytes) -> bytes:
    s = [b ^ k for b, k in zip(block, rk[0])]
    for rnd in range(1, 11):
        s = [_SBOX[b] for b in s]
        s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]  # shift rows
        if rnd != 10:
            n = []
            for c in range(4):
                a0, a1, a2, a3 = s[4 * c : 4 * c + 4]
                n += [
                    _M2[a0] ^ _M3[a1] ^ a2 ^ a3,
                    a0 ^ _M2[a1] ^ _M3[a2] ^ a3,
                    a0 ^ a1 ^ _M2[a2] ^ _M3[a3],
                    _M3[a0] ^ a1 ^ a2 ^ _M2[a3],
                ]
            s = n
        s = [b ^ k for b, k in zip(s, rk[rnd])]
    return bytes(s)


def _decrypt_block(rk, block: bytes) -> bytes:
    s = [b ^ k for b, k in zip(block, rk[10])]
    for rnd in range(9, -1, -1):
        s = [s[(i - 4 * (i % 4)) % 16] for i in range(16)]  # inverse shift rows
        s = [_INV_SBOX[b] for b in s]
        s = [b ^ k for b, k in zip(s, rk[rnd])]
        if rnd != 0:
            n = []
            for c in range(4):
                a0, a1, a2, a3 = s[4 * c : 4 * c + 4]
                n += [
                    _M14[a0] ^ _M11[a1] ^ _M13[a2] ^ _M9[a3],
                    _M9[a0] ^ _M14[a1] ^ _M11[a2] ^ _M13[a3],
                    _M13[a0] ^ _M9[a1] ^ _M14[a2] ^ _M11[a3],
                    _M11[a0] ^ _M13[a1] ^ _M9[a2] ^ _M14[a3],
                ]
            s = n
    return bytes(s)


def _unpad(data: bytes) -> bytes:
    if data and 1 <= data[-1] <= 16 and data.endswith(bytes([data[-1]]) * data[-1]):
        return data[: -data[-1]]
    return data


def _py_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    rk = _expand_key(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data) - len(data) % 16, 16):
        block = data[i : i + 16]
        out += bytes(a ^ b for a, b in zip(_decrypt_block(rk, block), prev))
        prev = block
    return _unpad(bytes(out))


def py_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128-CBC + PKCS7 encrypt (used by the tests to build encrypted streams)."""
    rk = _expand_key(key)
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        prev = _encrypt_block(rk, bytes(a ^ b for a, b in zip(data[i : i + 16], prev)))
        out += prev
    return bytes(out)


# ----------------------------------------------------------- Windows bcrypt (fast)
_win_decrypt = None
if sys.platform == "win32":
    try:
        import ctypes
        from ctypes import byref, c_ulong, c_void_p, create_string_buffer

        _bcrypt = ctypes.WinDLL("bcrypt")
        _alg = c_void_p()
        if _bcrypt.BCryptOpenAlgorithmProvider(byref(_alg), "AES", None, 0) != 0:
            raise OSError("BCryptOpenAlgorithmProvider failed")
        _mode = "ChainingModeCBC\0".encode("utf-16-le")
        if _bcrypt.BCryptSetProperty(_alg, "ChainingMode", _mode, len(_mode), 0) != 0:
            raise OSError("BCryptSetProperty failed")
        _BLOCK_PADDING = 1

        def _win_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:  # noqa: F811
            hkey = c_void_p()
            if _bcrypt.BCryptGenerateSymmetricKey(_alg, byref(hkey), None, 0, key, len(key), 0) != 0:
                raise OSError("BCryptGenerateSymmetricKey failed")
            try:
                ivbuf = create_string_buffer(iv, 16)
                out = create_string_buffer(len(data))
                n = c_ulong()
                rc = _bcrypt.BCryptDecrypt(hkey, data, len(data), None, ivbuf, 16, out, len(data),
                                           byref(n), _BLOCK_PADDING)
                if rc != 0:
                    # Bad padding etc. - retry without unpadding and strip manually.
                    ivbuf = create_string_buffer(iv, 16)
                    usable = len(data) - len(data) % 16
                    rc = _bcrypt.BCryptDecrypt(hkey, data, usable, None, ivbuf, 16, out, usable, byref(n), 0)
                    if rc != 0:
                        raise OSError(f"BCryptDecrypt failed (0x{rc & 0xFFFFFFFF:08x})")
                    return _unpad(out.raw[: n.value])
                return out.raw[: n.value]
            finally:
                _bcrypt.BCryptDestroyKey(hkey)

    except (OSError, AttributeError):
        _win_decrypt = None


def decrypt_cbc(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128-CBC decrypt with PKCS7 padding removed."""
    if len(key) != 16 or len(iv) != 16:
        raise ValueError("AES-128 needs a 16-byte key and IV")
    if _win_decrypt is not None:
        return _win_decrypt(key, iv, data)
    return _py_decrypt(key, iv, data)


FAST = _win_decrypt is not None
