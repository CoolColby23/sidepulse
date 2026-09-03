from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sidepulse.cli import sidepulse_main  # noqa: E402
from sidepulse.links import (  # noqa: E402
    IOSLink,
    LinkError,
    iter_sse_messages,
    load_ios_links,
    new_pairing_channel,
    normalize_apns_token,
    pairing_url,
    parse_ios_registration,
    render_pairing_qr_page,
    render_terminal_qr,
    save_ios_links,
    store_ios_link,
)


TOKEN_A = "a" * 64
TOKEN_B = "b" * 64


class LinkStorageTests(unittest.TestCase):
    def test_token_is_normalized_and_validated(self) -> None:
        self.assertEqual(normalize_apns_token("AA " * 32), TOKEN_A)
        for invalid in ("", "a" * 63, "g" * 64):
            with self.subTest(invalid=invalid):
                with self.assertRaises(LinkError):
                    normalize_apns_token(invalid)

    def test_links_round_trip_with_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "links.json"
            link = IOSLink("Peter's iPhone", TOKEN_A, linked_at="now")
            save_ios_links((link,), path)
            self.assertEqual(load_ios_links(path), (link,))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_relinking_a_token_updates_instead_of_duplicating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "links.json"
            store_ios_link(IOSLink("Old name", TOKEN_A), path)
            stored = store_ios_link(IOSLink("New name", TOKEN_A), path)
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0].name, "New name")

    def test_pairing_channel_is_an_eleven_character_base64url_secret(self) -> None:
        channels = {new_pairing_channel() for _ in range(100)}
        self.assertEqual(len(channels), 100)
        self.assertTrue(all(len(channel) == 11 for channel in channels))
        alphabet = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        self.assertTrue(all(set(channel) <= alphabet for channel in channels))

    def test_default_pairing_url_contains_only_the_compact_channel(self) -> None:
        channel = "7kP2_xQ9mLs"
        url = pairing_url("https://bridge.sidepulse.io/", channel, "Studio Mac")
        self.assertEqual(url, "sidepulse://p/7kP2_xQ9mLs")

    def test_custom_server_pairing_url_keeps_connection_details(self) -> None:
        url = pairing_url("https://bridge.example.com", "7kP2_xQ9mLs", "Studio Mac")
        self.assertIn("sidepulse://pair?", url)
        self.assertIn("server=https%3A%2F%2Fbridge.example.com", url)
        self.assertIn("channel=7kP2_xQ9mLs", url)
        self.assertIn("sender=Studio+Mac", url)

    def test_pairing_qr_renders_without_printing_the_raw_link(self) -> None:
        url = pairing_url(
            "https://bridge.sidepulse.io",
            "7kP2_xQ9mLs",
            "Studio Mac",
        )
        rendered = render_terminal_qr(url)
        lines = rendered.splitlines()
        self.assertTrue(any(character in rendered for character in "▀▄█"))
        self.assertLessEqual(len(lines), 20)
        self.assertLessEqual(max(map(len, lines)), 40)
        self.assertNotIn("sidepulse://", rendered)

    def test_pairing_qr_uses_solid_backgrounds_in_ansi_terminals(self) -> None:
        rendered = render_terminal_qr("sidepulse://p/7kP2_xQ9mLs", ansi=True)
        self.assertIn("\033[107m", rendered)
        self.assertIn("\033[40m", rendered)
        self.assertTrue(all(line.endswith("\033[0m") for line in rendered.splitlines()))

    def test_pairing_qr_page_is_vector_and_does_not_expose_the_link(self) -> None:
        rendered = render_pairing_qr_page("sidepulse://p/7kP2_xQ9mLs")
        self.assertIn("<svg", rendered)
        self.assertIn('shape-rendering="crispEdges"', rendered)
        self.assertIn("<path", rendered)
        self.assertNotIn("sidepulse://", rendered)

    def test_sse_parser_returns_complete_data_events(self) -> None:
        response = iter(
            [
                b": keepalive\n",
                b"data: first\n",
                b"data: second\n",
                b"\n",
            ]
        )
        self.assertEqual(list(iter_sse_messages(response)), ["first\nsecond"])

    def test_registration_response_becomes_a_link(self) -> None:
        response = json.dumps(
            {
                "v": 1,
                "type": "ios_registration",
                "device": {
                    "name": "Peter's iPhone",
                    "platform": "ios",
                    "bundle_id": "io.sidepulse.ios",
                    "push_token": TOKEN_B.upper(),
                },
            }
        )
        link = parse_ios_registration(response, server="https://bridge.sidepulse.io")
        self.assertEqual(link.name, "Peter's iPhone")
        self.assertEqual(link.token, TOKEN_B)


class WriteFallbackTests(unittest.TestCase):
    def test_write_uses_all_linked_phones_when_no_local_device_exists(self) -> None:
        links = (IOSLink("Phone A", TOKEN_A), IOSLink("Phone B", TOKEN_B))
        sent: list[tuple[str, str, str | None]] = []

        def send(link: IOSLink, program: str, *, event_id: str | None = None) -> str:
            sent.append((link.name, program, event_id))
            return "OK"

        stdout = io.StringIO()
        with (
            patch("sidepulse.cli.discover_devices", return_value=[]),
            patch("sidepulse.cli.load_ios_links", return_value=links),
            patch("sidepulse.cli.send_ios_program", side_effect=send),
            patch("sys.stdout", stdout),
        ):
            result = sidepulse_main(["write", r"off\nrepeat"])

        self.assertEqual(result, 0)
        self.assertEqual([item[0] for item in sent], ["Phone A", "Phone B"])
        self.assertTrue(all(item[1] == "off\nrepeat" for item in sent))
        self.assertEqual(len({item[2] for item in sent}), 1)

    def test_write_does_not_contact_phone_when_local_device_exists(self) -> None:
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "SidePulseDot"
            root.mkdir()
            candidate = type("Candidate", (), {"root": root, "target": root / "LEDS.LED"})()
            with (
                patch("sidepulse.cli.discover_devices", return_value=[candidate]),
                patch("sidepulse.cli.send_ios_program") as send,
                patch("sys.stdout", stdout),
            ):
                result = sidepulse_main(["write", "off", "--device", str(root)])
            self.assertEqual(result, 0)
            send.assert_not_called()
            self.assertEqual((root / "LEDS.LED").read_text(), "off")

    def test_missing_local_and_linked_devices_points_to_link_command(self) -> None:
        stderr = io.StringIO()
        with (
            patch("sidepulse.cli.discover_devices", return_value=[]),
            patch("sidepulse.cli.load_ios_links", return_value=()),
            patch("sys.stderr", stderr),
        ):
            result = sidepulse_main(["write", "off"])
        self.assertEqual(result, 2)
        self.assertIn("sidepulse link", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
