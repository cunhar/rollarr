import unittest

from services.release_selector import parse_release, select_releases


class TestReleaseSelector(unittest.TestCase):
    def test_parse_release_fields(self):
        p = parse_release("Severance.S02E03.PROPER.2160p.ATVP.WEB-DL.DDP5.1.Atmos.HEVC-GRP")
        self.assertEqual(p['resolution'], '2160p')
        self.assertEqual(p['source'], 'webdl')
        self.assertEqual(p['audio'], 'atmos')
        self.assertEqual(p['codec'], 'x265')
        self.assertTrue(p['is_proper'])
        self.assertFalse(p['is_junk_tag'])
        self.assertEqual(p['season'], 2)
        self.assertEqual(p['episode'], 3)

    def test_junk_filter_rejects_bad_releases(self):
        candidates = [
            {'title': 'Dune.Part.Two.2024.1080p.HDCAM.AAC-BAD', 'guid': '1', 'nzb_url': 'http://x/1', 'size_bytes': 2 * 1024**3},
            {'title': 'Dune.Part.Two.2024.1080p.WEB-DL.KORSUB.x264', 'guid': '2', 'nzb_url': 'http://x/2', 'size_bytes': 3 * 1024**3},
            {'title': 'Dune.Part.Two.2024.1080p.WEB-DL.DDP5.1.SAMPLE', 'guid': '3', 'nzb_url': 'http://x/3', 'size_bytes': 2 * 1024**3},
            {'title': 'Dune.Part.Two.2024.1080p.BluRay.x264', 'guid': '4', 'nzb_url': 'http://x/4', 'size_bytes': 50 * 1024**2},  # too small
            {'title': 'Dune.Part.Two.2024.2160p.REMUX.TrueHD', 'guid': '5', 'nzb_url': 'http://x/5', 'size_bytes': 80 * 1024**3},  # too large (>25GB)
            {'title': 'Dune.Part.Two.2024.1080p.BluRay.DTS-HD', 'guid': '6', 'nzb_url': 'http://x/6', 'size_bytes': 8 * 1024**3, 'passworded': True},
            {'title': 'Dune.Part.Two.2024.1080p.WEB-DL.DDP5.1.H264-GOOD', 'guid': '7', 'nzb_url': 'http://x/7', 'size_bytes': 6 * 1024**3},
        ]
        ranked = select_releases(
            candidates, 'movie', {'title': 'Dune: Part Two', 'year': 2024},
            preferred_resolution='1080p', preferred_audio='ddp', max_size_gb=25,
        )
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]['guid'], '7')

    def test_preferred_resolution_and_audio_ranking(self):
        candidates = [
            {'title': 'The.Bear.S01E02.720p.WEB-DL.AAC-A', 'guid': 'a', 'nzb_url': 'u', 'size_bytes': 500 * 1024**2, 'grabs': 10},
            {'title': 'The.Bear.S01E02.1080p.WEB-DL.AAC-B', 'guid': 'b', 'nzb_url': 'u', 'size_bytes': 1200 * 1024**2, 'grabs': 10},
            {'title': 'The.Bear.S01E02.1080p.WEB-DL.DDP5.1-C', 'guid': 'c', 'nzb_url': 'u', 'size_bytes': 1400 * 1024**2, 'grabs': 10},
            {'title': 'The.Bear.S01E02.PROPER.1080p.WEB-DL.DDP5.1-D', 'guid': 'd', 'nzb_url': 'u', 'size_bytes': 1400 * 1024**2, 'grabs': 10},
            {'title': 'The.Bear.S01E03.1080p.WEB-DL.DDP5.1-WRONG', 'guid': 'e', 'nzb_url': 'u', 'size_bytes': 1400 * 1024**2, 'grabs': 99},
            {'title': 'The.Bear.S01.1080p.WEB-DL.DDP5.1-PACK', 'guid': 'f', 'nzb_url': 'u', 'size_bytes': 5000 * 1024**2, 'grabs': 99},
        ]
        ranked = select_releases(
            candidates, 'episode', {'title': 'The Bear', 'season': 1, 'episode': 2},
            preferred_resolution='1080p', preferred_audio='ddp', max_size_gb=8,
        )
        guids = [r['guid'] for r in ranked]
        self.assertEqual(guids, ['d', 'c', 'b', 'a'])

        # Blocklist top pick -> second pick rises to #1
        ranked2 = select_releases(
            candidates, 'episode', {'title': 'The Bear', 'season': 1, 'episode': 2},
            blocklisted_guids={'d'},
            preferred_resolution='1080p', preferred_audio='ddp', max_size_gb=8,
        )
        self.assertEqual(ranked2[0]['guid'], 'c')

    def test_dv_without_hdr_and_reject_keywords(self):
        candidates = [
            {'title': 'Severance.S02E01.2160p.ATVP.WEB-DL.DV.H.265-A', 'guid': 'dv_only', 'nzb_url': 'u', 'size_bytes': 4 * 1024**3},
            {'title': 'Severance.S02E01.2160p.ATVP.WEB-DL.DV.HDR10.H.265-B', 'guid': 'dv_hdr', 'nzb_url': 'u', 'size_bytes': 4 * 1024**3},
            {'title': 'Severance.S02E01.1080p.MULTI.WEB-DL.H.264-C', 'guid': 'multi', 'nzb_url': 'u', 'size_bytes': 2 * 1024**3},
            {'title': 'Severance.S02E01.1080p.WEB-DL.AV1-D', 'guid': 'av1', 'nzb_url': 'u', 'size_bytes': 2 * 1024**3},
        ]
        ranked = select_releases(
            candidates, 'episode', {'title': 'Severance', 'season': 2, 'episode': 1},
            preferred_resolution='2160p', max_size_gb=10,
            reject_dv_without_hdr=True, reject_keywords='MULTI, AV1',
        )
        self.assertEqual([r['guid'] for r in ranked], ['dv_hdr'])


if __name__ == '__main__':
    unittest.main()
