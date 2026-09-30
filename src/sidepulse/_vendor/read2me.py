#!/usr/bin/env python3
"""Send SidePulse files using only read requests to setup.html (macOS)."""
from __future__ import annotations

import binascii
import ctypes
import os
from pathlib import Path
import secrets
import struct
import sys
import time

FIRMWARE_REBOOT_QUIET_SECONDS = 5

START = (16, 16, 1, 2, 3, 4)
STOP = (17, 17, 4, 3, 2, 1)
STATES = ("IDLE", "RECEIVING", "VALIDATING", "DONE", "ERROR")
ERRORS = ("NONE", "FRAME", "CRC", "HEADER", "OFFSET", "CONFLICT", "BUSY", "APPLY", "OVERFLOW")


def crc16(data: bytes) -> int:
    return binascii.crc_hqx(data, 0xffff)


def frame(transfer_id: int, name: str, total: int, offset: int, data: bytes) -> list[int]:
    if not 1 <= transfer_id <= 0xffffffff or not 1 <= total <= 0xffffffff:
        raise ValueError("transfer ID and total must be nonzero u32 values")
    filename = name.upper().encode("ascii")
    if filename not in (b"LEDS.LED", b"INIT.LED", b"FIRMWARE.BIN", b"SIDEPULSE_OTA.BIN"):
        raise ValueError("unsupported destination")
    if filename in (b"LEDS.LED", b"INIT.LED") and total > 512:
        raise ValueError("LED programs are limited to 512 bytes")
    if not 1 <= len(data) <= 512 or not 0 <= offset < total or offset + len(data) > total:
        raise ValueError("invalid chunk range")
    packet = struct.pack(">BIB", 1, transfer_id, len(filename)) + filename
    packet += struct.pack(">IIH", total, offset, len(data)) + data
    packet += struct.pack(">H", crc16(packet))
    return [*START, *(n for b in packet for n in (b >> 4, b & 15)), *STOP]


def decode_status(data: bytes) -> dict:
    """Decode one 512-byte setup.html sector, including its status CRC."""
    prefix = b"<!--READ2ME1:"
    start = 256 + len(prefix)
    if len(data) != 512 or data[256:start] != prefix or data[start + 128:start + 132] != b"-->\n":
        raise ValueError("invalid setup.html status envelope")
    encoded = data[start:start + 128]
    if any(c not in b"0123456789abcdef" for c in encoded):
        raise ValueError("invalid setup.html status hex")
    data = bytes.fromhex(encoded.decode('ascii'))
    if len(data) < 64 or data[:8] != b"READ2ME1" or data[8] != 1:
        raise ValueError("not a READ2ME v1 response; stale cache or firmware without READ2ME")
    if crc16(data[:62]) != struct.unpack_from(">H", data, 62)[0]:
        raise ValueError("torn status response")
    if data[9] >= len(STATES) or data[10] >= len(ERRORS) or data[11] > 2:
        raise ValueError("unknown status state, error, or target")
    fields = struct.unpack_from(">8I", data, 12)
    result = dict(zip(("transfer_id", "total", "received", "accepted_chunks", "errors", "symbols", "overflows", "capacity"), fields))
    result.update(state=STATES[data[9]], error=ERRORS[data[10]], target=data[11],
                  payload_crc16=struct.unpack_from(">H", data, 44)[0],
                  uptime_ms=struct.unpack_from(">I", data, 46)[0],
                  read_requests=struct.unpack_from(">I", data, 50)[0],
                  last_read_lba=struct.unpack_from(">I", data, 54)[0])
    return result


class _MacFile:
    """Read-only, aligned positional I/O; also used for fresh ordinary file reads."""
    def __init__(self, path: Path, expected_size=None):
        if sys.platform != "darwin":
            raise OSError("READ2ME hardware transport currently supports macOS only")
        import fcntl
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.posix_memalign.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t, ctypes.c_size_t]
        self.libc.pread.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_longlong]
        self.libc.pread.restype = ctypes.c_ssize_t
        self.libc.free.argtypes = [ctypes.c_void_p]
        self.buffer = ctypes.c_void_p()
        self.fd = os.open(path, os.O_RDONLY)
        try:
            if expected_size is not None and os.fstat(self.fd).st_size != expected_size:
                raise ValueError(f"control file must be exactly {expected_size} bytes")
            fcntl.fcntl(self.fd, 48, 1)  # Darwin F_NOCACHE
            fcntl.fcntl(self.fd, 45, 0)  # Darwin F_RDAHEAD
            # F_NOCACHE alone is insufficient: an unaligned Python bytes buffer
            # makes macOS read surrounding sectors, corrupting address symbols.
            page_size = os.sysconf("SC_PAGESIZE")
            result = self.libc.posix_memalign(ctypes.byref(self.buffer), page_size, page_size)
            if result:
                raise OSError(result, os.strerror(result))
        except BaseException:
            os.close(self.fd)
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.fd < 0:
            return
        self.libc.free(self.buffer)
        self.buffer = ctypes.c_void_p()
        os.close(self.fd)
        self.fd = -1

    def invalidate_cache(self):
        """Evict existing cached pages before reading an ordinary status file."""
        if self.fd < 0:
            raise ValueError("channel is closed")
        size = os.fstat(self.fd).st_size
        if size == 0:
            return
        self.libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_longlong]
        self.libc.mmap.restype = ctypes.c_void_p
        self.libc.msync.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        self.libc.msync.restype = ctypes.c_int
        self.libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        self.libc.munmap.restype = ctypes.c_int
        # PROT_READ | MAP_SHARED; never access the mapping itself.
        address = self.libc.mmap(None, size, 1, 1, self.fd, 0)
        if address == ctypes.c_void_p(-1).value:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        try:
            if self.libc.msync(address, size, 2) != 0:  # MS_INVALIDATE
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
        finally:
            if self.libc.munmap(address, size) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))

    def read_at(self, offset: int) -> bytes:
        if self.fd < 0:
            raise ValueError("channel is closed")
        count = self.libc.pread(self.fd, self.buffer, 512, offset)
        if count < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return ctypes.string_at(self.buffer, count)


class Channel(_MacFile):
    """Synchronous READ2ME v1 sender. Use as a context manager; serialize callers."""
    def __init__(self, path: Path):
        super().__init__(path, 19 * 512)
        self._cache_mapping = None
        try:
            self.libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_longlong]
            self.libc.mmap.restype = ctypes.c_void_p
            self.libc.msync.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
            self.libc.msync.restype = ctypes.c_int
            self.libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            self.libc.munmap.restype = ctypes.c_int
            # PROT_READ | MAP_SHARED. Never touch the mapping: doing so would
            # fault in pages and issue unwanted read-address symbols.
            address = self.libc.mmap(None, 19 * 512, 1, 1, self.fd, 0)
            if address == ctypes.c_void_p(-1).value:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
            self._cache_mapping = address
        except BaseException:
            self.close()
            raise

    def close(self):
        address = getattr(self, '_cache_mapping', None)
        self._cache_mapping = None
        try:
            if address is not None and self.libc.munmap(address, 19 * 512) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
        finally:
            super().close()

    def _invalidate_cache(self):
        if self.fd < 0:
            raise ValueError("channel is closed")
        # F_NOCACHE does not evict pages previously populated by cat/Finder.
        # MS_INVALIDATE drops the control file's cached pages without reading
        # through the mapping. Do this before every read, even on reused handles.
        if self.libc.msync(self._cache_mapping, 19 * 512, 2) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))

    def read(self, symbol: int) -> bytes:
        if not 0 <= symbol <= 18:
            raise ValueError("invalid symbol")
        self._invalidate_cache()
        data = self.read_at(symbol * 512)
        if len(data) != 512:
            raise OSError("short control-file read")
        return data

    def status(self) -> dict:
        for _ in range(10):
            try:
                return decode_status(self.read(18))
            except ValueError:
                time.sleep(0.01)
        return decode_status(self.read(18))

    def send_frame(self, symbols: list[int]):
        for symbol in symbols:
            self.read(symbol)

    def probe(self) -> dict:
        """Check that control-address reads cause observable receiver progress."""
        before = self.status()
        # A partial START resets framing but cannot commit any command.
        self.send_frame([*START, 16])
        deadline = time.monotonic() + 1
        while True:
            after = self.status()
            delta = (after["symbols"] - before["symbols"]) & 0xffffffff
            if delta > len(START) + 1:
                raise RuntimeError("Unexpected extra control symbols; another reader or read aggregation is interfering")
            if delta == len(START) + 1 and after["read_requests"] != before["read_requests"]:
                return after
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Control reads produced no fresh symbol acknowledgement. "
                    "Filesystem/reader caching or read aggregation may prevent this channel; "
                    "F_NOCACHE alone is not sufficient on every Mac. No upload was started."
                )
            time.sleep(0.01)

    def upload(self, data: bytes, name: str, transfer_id: int | None = None,
               chunk_size: int = 512, retries: int = 3, progress=None,
               ack_timeout: float = 2, completion_timeout: float = 30) -> dict:
        name = name.upper()
        # Validate all inputs before the probe changes receiver framing.
        frame(transfer_id if transfer_id is not None else 1, name, len(data), 0, data[:512])
        if ack_timeout < 0 or completion_timeout < 0:
            raise ValueError("timeouts cannot be negative")
        if retries < 0:
            raise ValueError("retries cannot be negative")
        initial = self.status()
        if initial["state"] == "VALIDATING":
            raise RuntimeError("device is busy validating another transfer")
        if transfer_id is not None and transfer_id == initial["transfer_id"]:
            raise ValueError("use a fresh transfer ID for a new upload")
        if not 0 < len(data) <= initial["capacity"]:
            raise ValueError("payload exceeds receiver capacity or is empty")
        if name.upper() in ("LEDS.LED", "INIT.LED") and len(data) > 512:
            raise ValueError("LED programs are limited to 512 bytes")
        if not 1 <= chunk_size <= 512:
            raise ValueError("chunk size must be 1..512")
        self.probe()
        if transfer_id is None:
            transfer_id = secrets.randbelow(0xffffffff) + 1
            while transfer_id == initial["transfer_id"]:
                transfer_id = secrets.randbelow(0xffffffff) + 1
        target = 0 if name == "LEDS.LED" else 1 if name == "INIT.LED" else 2
        def matches(status):
            return (status["transfer_id"] == transfer_id and status["total"] == len(data)
                    and status["target"] == target)

        final_firmware_frame = False
        try:
            for offset in range(0, len(data), chunk_size):
                chunk = data[offset:offset + chunk_size]
                symbols = frame(transfer_id, name, len(data), offset, chunk)
                if target == 2 and offset + len(chunk) == len(data):
                    final_firmware_frame = True
                for attempt in range(retries + 1):
                    self.send_frame(symbols)
                    deadline = time.monotonic() + ack_timeout
                    while True:
                        status = self.status()
                        if matches(status) and status["received"] >= offset + len(chunk):
                            break
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(0.01)
                    if matches(status) and status["received"] >= offset + len(chunk):
                        break
                    if attempt == retries:
                        raise RuntimeError(f"chunk at {offset} was not acknowledged: {status}")
                if progress and not (target == 2 and offset + len(chunk) == len(data)):
                    progress(offset + len(chunk), len(data))
            deadline = time.monotonic() + completion_timeout
            while status["state"] == "VALIDATING" and time.monotonic() < deadline:
                time.sleep(0.02)
                status = self.status()
            if not matches(status) or status["received"] != len(data) or status["state"] != "DONE" or status["error"] != "NONE":
                raise RuntimeError(f"upload did not finish successfully: {status}")
            if status["payload_crc16"] != crc16(data):
                raise RuntimeError("received payload checksum differs")
        finally:
            # Also stay quiet on an ambiguous final acknowledgement / I/O error:
            # the board may already be restarting. Never probe during this wait.
            if final_firmware_frame:
                time.sleep(FIRMWARE_REBOOT_QUIET_SECONDS)
        if target == 2 and progress:
            progress(len(data), len(data))
        return status
