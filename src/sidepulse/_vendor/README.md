# READ2ME

`read2me.py` is the dependency-free Python protocol client from
`sdstatus_bitbang/utils/read2me/python/sidepulse_read2me/protocol.py`, copied
2026-09-29 (source checkout HEAD bb5910ad713b2fd353f73f8358bc957a662fdd5e).
The shared interoperability vectors and protocol tests are included in this
repository. Keep the aligned reads, cache invalidation, freshness probe and
completion checks together when updating this client.

`sidepulse/usb_control.py` is adapted from that checkout's
`utils/usb-control/sidepulse_usb_control.py`: packaged libusb discovery,
mount-to-USB selection, and ambiguous-write reconciliation are added here.

The application exposes these transports only for LED writes on firmware
1.1 or newer. Firmware updates are outside the automatic fallback API.
