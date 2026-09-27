#!/usr/bin/env python3
"""macOS: write an existing LEDS.LED on an unmounted, single-FAT FAT12 disk.

    sudo python3 scripts/write_led_raw.py /dev/rdisk4 '#ff00ff'
    sudo python3 scripts/write_led_raw.py /dev/disk4 - < animation.LED

Only whole devices <= 300,000 bytes are accepted. No mounting or allocation.
"""

import argparse
import fcntl
import os
import plistlib
import re
import stat
import struct
import subprocess
import sys

MAX_DISK_BYTES = 300_000  # Decimal KB; deliberately stricter than 300 KiB.
SECTOR = 512
# macOS <sys/disk.h>: _IOR('d', 24, uint32_t), _IOR('d', 25, uint64_t).
DKIOCGETBLOCKSIZE = 0x40046418
DKIOCGETBLOCKCOUNT = 0x40086419


def require(condition, message):
    if not condition:
        raise ValueError(message)


def device_size(fd):
    """Query actual media capacity, never the size claimed by its FAT boot sector."""
    require(sys.platform == "darwin", "This utility requires macOS.")
    mode = os.fstat(fd).st_mode
    require(stat.S_ISBLK(mode) or stat.S_ISCHR(mode), "Not a disk device.")
    block = struct.unpack("=I", fcntl.ioctl(fd, DKIOCGETBLOCKSIZE, bytes(4)))[0]
    count = struct.unpack("=Q", fcntl.ioctl(fd, DKIOCGETBLOCKCOUNT, bytes(8)))[0]
    size = block * count
    require(0 < size <= MAX_DISK_BYTES,
            f"Refusing drive size {size:,} bytes; limit is {MAX_DISK_BYTES:,}.")
    require(block == SECTOR, "Only 512-byte device blocks are supported.")
    return size


def check_unmounted(device):
    result = subprocess.run(
        ["/usr/sbin/diskutil", "info", "-plist", device],
        check=True, capture_output=True,
    )
    info = plistlib.loads(result.stdout)
    require(info.get("DeviceIdentifier") == device.removeprefix("/dev/"),
            "Cannot verify device identity.")
    require(info.get("WholeDisk") is True, "Only whole disks are accepted.")
    # diskutil omits MountPoint for unmounted media; there is no Mounted key.
    require(info.get("MountPoint", "") == "",
            f"Drive must be unmounted first: diskutil unmountDisk {device}")


def read_sector(fd, offset):
    data = os.pread(fd, SECTOR, offset)
    require(len(data) == SECTOR, "Short disk read; refusing to write.")
    return data


def locate_led(fd, size):
    """Return data offset, directory-sector offset, and entry offset within it."""
    boot = read_sector(fd, 0)
    bps, spc, reserved, fats, entries, total = struct.unpack_from("<HBHBHH", boot, 11)
    fat_sectors = struct.unpack_from("<H", boot, 22)[0]
    hidden, total32 = struct.unpack_from("<II", boot, 28)
    total = total or total32
    require(boot[510:512] == b"\x55\xaa" and bps == SECTOR and fats == 1,
            "Expected FAT12 with 512-byte sectors and exactly one FAT.")
    require(hidden == 0 and reserved > 0 and entries > 0 and fat_sectors > 0,
            "Expected a FAT12 volume starting at sector zero.")
    require(spc in (1, 2, 4, 8, 16, 32, 64, 128), "Invalid cluster size.")
    root = reserved + fat_sectors
    data = root + (entries * 32 + SECTOR - 1) // SECTOR
    clusters = (total - data) // spc
    require(0 < total * SECTOR <= size and 0 < clusters < 4085,
            "Invalid FAT12 geometry or filesystem exceeds device size.")
    require(((clusters + 2) * 3 + 1) // 2 <= fat_sectors * SECTOR,
            "FAT is too small for this volume.")
    fat = os.pread(fd, fat_sectors * SECTOR, reserved * SECTOR)
    require(len(fat) == fat_sectors * SECTOR, "Short FAT read.")
    for index in range(entries):
        if index % 16 == 0:
            directory = read_sector(fd, root * SECTOR + index * 32)
        offset = (index % 16) * 32
        entry = directory[offset:offset + 32]
        if entry[0] == 0:
            break
        if entry[:11] != b"LEDS    LED" or entry[11] & 0x18:
            continue
        require(not entry[11] & 1, "LEDS.LED is read-only.")
        cluster = struct.unpack_from("<H", entry, 26)[0]
        require(2 <= cluster < clusters + 2, "Invalid LEDS.LED cluster.")
        packed = struct.unpack_from("<H", fat, cluster + cluster // 2)[0]
        next_cluster = (packed >> (4 if cluster & 1 else 0)) & 0xFFF
        require(next_cluster >= 0xFF8 or
                (2 <= next_cluster < clusters + 2 and next_cluster != cluster),
                "LEDS.LED cluster is unallocated or invalid.")
        return ((data + (cluster - 2) * spc) * SECTOR,
                (root + index // 16) * SECTOR, offset)
    raise ValueError("Existing LEDS.LED not found in the root directory.")


def write_led(device, text):
    require(sys.platform == "darwin", "This utility requires macOS.")
    match = re.fullmatch(r"/dev/r?(disk[0-9]+)", device)
    require(match is not None, "Use a whole /dev/diskN or /dev/rdiskN; no partitions.")
    payload = text.encode("utf-8")
    require(0 < len(payload) <= SECTOR and len(text.splitlines()) <= 20,
            "LED program must contain 1–512 bytes and at most 20 lines.")
    require("\0" not in text, "LED program must not contain NUL bytes.")
    buffered = "/dev/" + match.group(1)
    check_unmounted(buffered)
    # No symlinks, creation or truncation. Retain this descriptor for all I/O.
    fd = os.open(device, os.O_RDWR | os.O_NOFOLLOW)
    try:
        size = device_size(fd)
        data_offset, root_offset, entry_offset = locate_led(fd, size)
        directory = bytearray(read_sector(fd, root_offset))
        struct.pack_into("<I", directory, entry_offset + 28, len(payload))
        # Data first, file size last. Preserve the existing allocation and all
        # other directory entries. Raw devices require whole-sector writes.
        for offset, sector in ((data_offset, payload.ljust(SECTOR, b"\0")),
                               (root_offset, directory)):
            check_unmounted(buffered)
            require(device_size(fd) == size, "Device capacity changed; refusing write.")
            require(0 <= offset <= size - SECTOR and offset % SECTOR == 0,
                    "Write would be outside the device or unaligned.")
            require(os.pwrite(fd, sector, offset) == SECTOR, "Short disk write.")
            os.fsync(fd)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device", help="whole /dev/diskN or /dev/rdiskN")
    parser.add_argument("program", help="LED text (supports \\n), or - to read stdin")
    args = parser.parse_args()
    try:
        text = (sys.stdin.read(513) if args.program == "-"
                else args.program.replace("\\n", "\n"))
        write_led(args.device, text)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"error: {exc}\n")
    print(f"Wrote LEDS.LED to {args.device}")


if __name__ == "__main__":
    main()
