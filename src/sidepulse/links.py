from __future__ import annotations

import json
import os
import queue
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .settings import default_config_dir


DEFAULT_BRIDGE_SERVER = "https://bridge.sidepulse.io"
PAIRING_TIMEOUT_SECONDS = 5 * 60
IOS_BUNDLE_ID = "io.sidepulse.ios"
APNS_TOKEN_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class LinkError(RuntimeError):
    pass


@dataclass(frozen=True)
class IOSLink:
    name: str
    token: str
    server: str = DEFAULT_BRIDGE_SERVER
    linked_at: str = ""

    @property
    def link_id(self) -> str:
        return self.token[:12]

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.link_id,
            "name": self.name,
            "token": self.token,
            "server": self.server,
            "linked_at": self.linked_at,
        }


def default_links_path() -> Path:
    return default_config_dir() / "links.json"


def bridge_server() -> str:
    return normalize_server(os.environ.get("SIDEPULSE_SERVER", DEFAULT_BRIDGE_SERVER))


def normalize_server(value: str) -> str:
    text = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LinkError("Bridge server must be an HTTP or HTTPS origin.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LinkError("Bridge server must not contain credentials, a query, or a fragment.")
    if parsed.path not in {"", "/"}:
        raise LinkError("Bridge server must be an origin without a path.")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def normalize_apns_token(value: str) -> str:
    token = "".join(value.split()).lower()
    if not APNS_TOKEN_PATTERN.fullmatch(token):
        raise LinkError("Push token must be exactly 64 hexadecimal characters.")
    return token


def load_ios_links(path: Path | None = None) -> tuple[IOSLink, ...]:
    target = (path or default_links_path()).expanduser()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except Exception:
        return ()
    if not isinstance(data, dict) or data.get("version") != 1:
        return ()
    items = data.get("ios")
    if not isinstance(items, list):
        return ()
    links: list[IOSLink] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            links.append(
                IOSLink(
                    name=str(item.get("name") or "iPhone"),
                    token=normalize_apns_token(str(item["token"])),
                    server=normalize_server(str(item.get("server") or DEFAULT_BRIDGE_SERVER)),
                    linked_at=str(item.get("linked_at") or ""),
                )
            )
        except (KeyError, LinkError):
            continue
    return tuple(links)


def save_ios_links(links: Iterable[IOSLink], path: Path | None = None) -> Path:
    target = (path or default_links_path()).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.parent.chmod(0o700)
    except OSError:
        pass
    payload = {
        "version": 1,
        "ios": [link.to_dict() for link in links],
    }
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(target)
    target.chmod(0o600)
    return target


def store_ios_link(link: IOSLink, path: Path | None = None) -> tuple[IOSLink, ...]:
    current = list(load_ios_links(path))
    updated: list[IOSLink] = []
    replaced = False
    for existing in current:
        if existing.token == link.token:
            updated.append(link)
            replaced = True
        else:
            updated.append(existing)
    if not replaced:
        updated.append(link)
    save_ios_links(updated, path)
    return tuple(updated)


def pairing_url(server: str, channel: str, sender: str | None = None) -> str:
    normalized_server = normalize_server(server)
    try:
        normalized_channel = str(uuid.UUID(channel))
    except ValueError as exc:
        raise LinkError("Pairing channel must be a UUID.") from exc
    query = {
        "v": "1",
        "server": normalized_server,
        "channel": normalized_channel,
    }
    clean_sender = (sender or socket.gethostname()).strip()[:80]
    if clean_sender:
        query["sender"] = clean_sender
    return "sidepulse://pair?" + urllib.parse.urlencode(query)


def render_terminal_qr(value: str) -> str:
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError as exc:  # pragma: no cover - packaging installs it.
        raise LinkError("QR support is unavailable; reinstall SidePulse and try again.") from exc

    code = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=1, border=2)
    code.add_data(value)
    code.make(fit=True)
    matrix = code.get_matrix()
    pixels = {
        (False, False): " ",
        (True, False): "▀",
        (False, True): "▄",
        (True, True): "█",
    }
    lines: list[str] = []
    for row_index in range(0, len(matrix), 2):
        top = matrix[row_index]
        bottom = matrix[row_index + 1] if row_index + 1 < len(matrix) else [False] * len(top)
        lines.append("".join(pixels[(top_cell, bottom_cell)] for top_cell, bottom_cell in zip(top, bottom)))
    return "\n".join(lines)


def parse_ios_registration(value: str, *, server: str) -> IOSLink:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise LinkError("Pairing response was not valid JSON.") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("type") != "ios_registration":
        raise LinkError("Pairing response has an unsupported format.")
    device = payload.get("device")
    if not isinstance(device, dict):
        raise LinkError("Pairing response did not include a device.")
    bundle_id = str(device.get("bundle_id") or "")
    if bundle_id != IOS_BUNDLE_ID:
        raise LinkError(f"The phone uses unsupported bundle ID {bundle_id or '(missing)' }.")
    name = str(device.get("name") or "iPhone").strip()[:80] or "iPhone"
    token = normalize_apns_token(str(device.get("push_token") or ""))
    return IOSLink(
        name=name,
        token=token,
        server=normalize_server(server),
        linked_at=datetime.now(timezone.utc).isoformat(),
    )


def iter_sse_messages(response) -> Iterable[str]:
    data_lines: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith("data:"):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            data_lines.append(value)
    if data_lines:
        yield "\n".join(data_lines)


def listen_for_ios_registration(
    server: str,
    channel: str,
    results: queue.Queue[IOSLink],
    stop: threading.Event,
    *,
    deadline: float,
) -> None:
    url = f"{normalize_server(server)}/api/leds/{urllib.parse.quote(channel, safe='')}"
    while not stop.is_set() and time.monotonic() < deadline:
        remaining = max(1.0, deadline - time.monotonic())
        request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        try:
            with urllib.request.urlopen(request, timeout=min(20.0, remaining)) as response:
                for message in iter_sse_messages(response):
                    if stop.is_set():
                        return
                    try:
                        link = parse_ios_registration(message, server=server)
                    except LinkError:
                        continue
                    results.put(link)
                    return
        except (OSError, urllib.error.URLError):
            if not stop.wait(0.5):
                continue


def send_ios_program(link: IOSLink, program: str, *, event_id: str | None = None) -> str:
    payload = {
        "leds": program,
        "data": {
            "sidepulse_event_id": event_id or str(uuid.uuid4()),
        },
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    url = f"{normalize_server(link.server)}/api/leds/apns_{link.token}"
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            return response.read().decode("utf-8", errors="replace").strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise LinkError(detail or f"Bridge returned HTTP {exc.code}.") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise LinkError(f"Could not reach the bridge: {exc}") from exc
