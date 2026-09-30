from unittest.mock import patch

import pytest

from sidepulse import keep_awake, status_reader


@pytest.fixture
def volume(tmp_path):
    (tmp_path / "STATUS.TXT").write_text("legacy status\n")
    return tmp_path


def test_readonly_keepalive_reads_status(volume):
    with patch.object(keep_awake.subprocess, "run") as run, patch.object(
        status_reader, "read_status_bytes"
    ) as read:
        controller = keep_awake.KeepAwakeController(status_read_async=False)
        assert controller.poke_status_file(volume / "LEDS.LED", now=0) == volume / "STATUS.TXT"
        read.assert_called_once_with(volume / "STATUS.TXT")
        run.assert_not_called()


def test_keepalive_reads_status_without_firmware_validation(volume):
    (volume / "STATUS.TXT").write_text("legacy status\n")
    with patch.object(status_reader.sys, "platform", "linux"):
        keep_awake.read_status_file(volume / "STATUS.TXT")
    assert not (volume / "keepalive").exists()


def test_keepalive_uses_uncached_mac_reader(volume):
    from sidepulse import status_reader as read2me

    with patch.object(status_reader.sys, "platform", "darwin"), patch.object(
        read2me, "_MacFile"
    ) as reader:
        reader.return_value.__enter__.return_value.read_at.return_value = b"status"
        keep_awake.read_status_file(volume / "STATUS.TXT")
        reader.assert_called_once_with(volume / "STATUS.TXT")
        reader.return_value.__enter__.return_value.invalidate_cache.assert_called_once_with()
        reader.return_value.__enter__.return_value.read_at.assert_called_once_with(0)


def test_keepalive_cache_invalidation_failure_prevents_read(volume):
    from sidepulse import status_reader as read2me

    with patch.object(status_reader.sys, "platform", "darwin"), patch.object(
        read2me, "_MacFile"
    ) as reader:
        stream = reader.return_value.__enter__.return_value
        stream.invalidate_cache.side_effect = OSError("invalidation failed")
        with pytest.raises(OSError, match="invalidation failed"):
            keep_awake.read_status_file(volume / "STATUS.TXT")
        stream.read_at.assert_not_called()
