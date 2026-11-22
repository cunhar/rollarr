import zlib
import unittest

from services.usenet_downloader import decode_yenc, parse_nzb


def _yenc_encode(payload: bytes) -> bytes:
    out = bytearray()
    for b in payload:
        enc = (b + 42) & 0xFF
        if enc in (0x00, 0x0A, 0x0D, 0x3D):  # NUL, LF, CR, '='
            out.append(0x3D)
            out.append((enc + 64) & 0xFF)
        else:
            out.append(enc)
    return bytes(out)


class TestYencAndNzb(unittest.TestCase):
    def test_yenc_decode_with_escapes_and_crc(self):
        original = bytes(range(256)) + b"Hello Rolarr Usenet!\r\n=special="
        crc_hex = f"{zlib.crc32(original) & 0xFFFFFFFF:08x}"
        encoded = _yenc_encode(original)
        article = (
            f"=ybegin part=1 line=128 size={len(original)} name=test.mkv\r\n"
            f"=ypart begin=1 end={len(original)}\r\n"
        ).encode('latin-1') + encoded + f"\r\n=yend size={len(original)} part=1 pcrc32={crc_hex}\r\n".encode('latin-1')

        res = decode_yenc(article)
        self.assertEqual(res['data'], original)
        self.assertEqual(res['name'], 'test.mkv')
        self.assertEqual(res['begin'], 0)
        self.assertTrue(res['crc_ok'])

    def test_parse_nzb_xml(self):
        sample_nzb = b"""<?xml version="1.0" encoding="utf-8"?>
        <nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
          <file poster="a@b.com" date="1700000000" subject='Show S01E01 [01/02] - "video.mkv" yEnc (1/2)'>
            <groups><group>alt.binaries.teevee</group></groups>
            <segments>
              <segment bytes="500000" number="2">seg2@news.example</segment>
              <segment bytes="500000" number="1">seg1@news.example</segment>
            </segments>
          </file>
        </nzb>"""
        files = parse_nzb(sample_nzb)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]['filename'], 'video.mkv')
        self.assertEqual(files[0]['total_bytes'], 1000000)
        self.assertEqual([s['number'] for s in files[0]['segments']], [1, 2])
        self.assertEqual(files[0]['segments'][0]['msgid'], 'seg1@news.example')


if __name__ == '__main__':
    unittest.main()
