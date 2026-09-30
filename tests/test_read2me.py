import errno
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace
from sidepulse._vendor.read2me import Channel, crc16, frame, decode_status
from sidepulse._vendor.read2me import START

VECTORS = dict(line.split('=',1) for line in (Path(__file__).parent/'fixtures'/'read2me-vectors.txt').read_text().splitlines() if not line.startswith('#'))

def envelope(raw):
    return b' ' * 256 + b'<!--READ2ME1:' + raw.hex().encode() + b'-->\n' + b' ' * 110 + b'\n'

class FakeChannel(Channel):
    def __init__(self, *, drop_ack=False, bad_crc=False, wrong_target=False, reject=False):
        self.s = decode_status(bytes.fromhex(VECTORS['html_status']))
        self.s.update(transfer_id=0,total=0,received=0,accepted_chunks=0,state='IDLE',symbols=0)
        self.payload = bytearray()
        self.sent = []
        self.drop_ack=drop_ack; self.bad_crc=bad_crc; self.wrong_target=wrong_target;self.reject=reject
        self.stale=None
    def status(self):
        if self.stale is not None:
            stale,self.stale=self.stale,None
            return stale
        return self.s.copy()
    def send_frame(self, symbols):
        self.s['symbols']+=len(symbols);self.s['read_requests']+=len(symbols)+1
        if symbols == [*START,16]: return
        self.sent.append(symbols)
        raw=bytes((a<<4)|b for a,b in zip(symbols[6:-6:2],symbols[7:-6:2]))
        assert crc16(raw[:-2])==int.from_bytes(raw[-2:],'big')
        old=self.s.copy()
        ident=int.from_bytes(raw[1:5],'big');n=raw[5];name=raw[6:6+n]
        total,offset,size=struct.unpack_from('>IIH',raw,6+n)
        data=raw[16+n:-2]
        if offset==len(self.payload):
            self.payload.extend(data);self.s['accepted_chunks']+=1
        else: assert bytes(self.payload[offset:offset+size])==data
        self.s.update(transfer_id=ident,total=total,received=len(self.payload),target=0 if name==b'LEDS.LED' else 1 if name==b'INIT.LED' else 2,
                      state='DONE' if len(self.payload)==total else 'RECEIVING',error='NONE',payload_crc16=crc16(self.payload))
        if self.bad_crc:self.s['payload_crc16']^=1
        if self.wrong_target:self.s['target']=2
        if self.reject:self.s.update(state='ERROR',error='APPLY')
        if self.drop_ack:self.stale=old;self.drop_ack=False

class ProtocolTests(unittest.TestCase):
    def test_vectors(self):
        self.assertEqual(crc16(b'123456789'),0x29b1)
        self.assertEqual(bytes(frame(0x01020304,'leds.led',3,0,b'off')).hex(),VECTORS['symbols'])
        s=decode_status(bytes.fromhex(VECTORS['html_status']))
        self.assertEqual((s['transfer_id'],s['payload_crc16'],s['uptime_ms']),(0x01020304,crc16(b'off'),123456))
    def test_invalid_frame(self):
        for args in [(0,'LEDS.LED',3,0,b'off'),(1,'BAD.LED',3,0,b'off'),(1,'INIT.LED',513,0,b'off'),(1,'LEDS.LED',3,2,b'off')]:
            with self.assertRaises(ValueError): frame(*args)
    def test_status_corruption_and_unknown_enum(self):
        for offset in [0,8,9,10,11,32,63]:
            s=bytearray.fromhex(VECTORS['status']);s[offset]=255
            if offset in (9,10,11):s[62:64]=crc16(s[:62]).to_bytes(2,'big')
            with self.assertRaises(ValueError):decode_status(envelope(s))
        for offset in [256, 269, 300, 397]:
            s=bytearray.fromhex(VECTORS['html_status']);s[offset]=255
            with self.assertRaises(ValueError):decode_status(s)
        for s in [b'', bytes.fromhex(VECTORS['status']), bytes.fromhex(VECTORS['html_status'])[:-1]]:
            with self.assertRaises(ValueError):decode_status(s)
    def test_chunking_and_lost_ack_retry(self):
        c=FakeChannel(drop_ack=True)
        s=c.upload(b'abcdef','LEDS.LED',transfer_id=42,chunk_size=3,ack_timeout=0)
        self.assertEqual(s['accepted_chunks'],2)
        self.assertEqual(c.sent[0],c.sent[1])
        self.assertEqual(s['received'],6)
    def test_failed_completion(self):
        for kwargs in ({'bad_crc':True},{'wrong_target':True},{'reject':True}):
            with self.assertRaises(RuntimeError):FakeChannel(**kwargs).upload(b'off','LEDS.LED',ack_timeout=0,retries=0)
    def test_firmware_quiet_before_return_and_final_progress(self):
        c=FakeChannel();events=[]
        with patch('sidepulse._vendor.read2me.time.sleep',side_effect=lambda seconds: events.append(('quiet',seconds))):
            status=c.upload(b'firmware','FIRMWARE.BIN',transfer_id=42,
                            progress=lambda n,total: events.append(('progress',n)))
        self.assertEqual(status['state'],'DONE')
        self.assertEqual(events,[('quiet',5),('progress',8)])
    def test_firmware_quiet_even_on_bad_crc_or_uncertain_final_read(self):
        with patch('sidepulse._vendor.read2me.time.sleep') as sleep:
            with self.assertRaises(RuntimeError):
                FakeChannel(bad_crc=True).upload(b'firmware','FIRMWARE.BIN',transfer_id=42)
            sleep.assert_called_once_with(5)
        c=FakeChannel();send=c.send_frame
        def disconnect(symbols):
            send(symbols)
            if c.s['state']=='DONE':raise OSError('disconnected after final frame')
        with patch.object(c,'send_frame',side_effect=disconnect),patch('sidepulse._vendor.read2me.time.sleep') as sleep:
            with self.assertRaises(OSError):c.upload(b'firmware','FIRMWARE.BIN',transfer_id=42)
            sleep.assert_called_once_with(5)
    def test_led_completion_has_no_reboot_wait(self):
        with patch('sidepulse._vendor.read2me.time.sleep') as sleep:
            FakeChannel().upload(b'off','LEDS.LED',transfer_id=42)
            sleep.assert_not_called()
    def test_cache_invalidation_failure_never_reads_a_symbol(self):
        c=Channel.__new__(Channel)
        c.fd=12;c._cache_mapping=4096
        c.libc=SimpleNamespace(msync=Mock(return_value=-1))
        c.read_at=Mock()
        with patch('sidepulse._vendor.read2me.ctypes.get_errno',return_value=errno.EIO):
            with self.assertRaises(OSError):c.read(18)
        c.read_at.assert_not_called()
        c.fd=-1
        with self.assertRaises(ValueError):c.read(18)
        c.libc.msync.assert_called_once()
    def test_invalid_inputs_before_probe(self):
        c=FakeChannel()
        with self.assertRaises(ValueError):c.upload(b'off','LEDS.LED',transfer_id=0)
        self.assertEqual(c.s['symbols'],0)
    def test_probe_rejects_extra_and_stale_symbols(self):
        c=FakeChannel()
        before=c.status();after=before.copy();after['symbols']+=8
        with patch.object(c,'status',side_effect=[before,after]):
            with self.assertRaises(RuntimeError):c.probe()
        with patch.object(c,'status',return_value=before),patch('sidepulse._vendor.read2me.time.monotonic',side_effect=[0,2]):
            with self.assertRaises(RuntimeError):c.probe()
