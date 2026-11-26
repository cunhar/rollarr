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

    def test_movie_title_and_year_normalization(self):
        # 1. "Dune: Part Two" matches "Dune.Part.2.2024..."
        c1 = [{'title': 'Dune.Part.2.2024.1080p.WEB-DL.DDP5.1.H264-GRP', 'guid': 'm1', 'nzb_url': 'u', 'size_bytes': 5 * 1024**3}]
        r1 = select_releases(c1, 'movie', {'title': 'Dune: Part Two', 'year': 2024})
        self.assertEqual(len(r1), 1)

        # 2. "Gladiator II" matches "Gladiator.2.2024..."
        c2 = [{'title': 'Gladiator.2.2024.1080p.WEB-DL.DDP5.1.H264-GRP', 'guid': 'm2', 'nzb_url': 'u', 'size_bytes': 5 * 1024**3}]
        r2 = select_releases(c2, 'movie', {'title': 'Gladiator II', 'year': 2024})
        self.assertEqual(len(r2), 1)

        # 3. "1917" (2019) with adjacent 4-digit year numbers in title
        c3 = [{'title': '1917.2019.1080p.BluRay.x264-GRP', 'guid': 'm3', 'nzb_url': 'u', 'size_bytes': 5 * 1024**3}]
        r3 = select_releases(c3, 'movie', {'title': '1917', 'year': 2019})
        self.assertEqual(len(r3), 1)

        # 4. "Deadpool & Wolverine" matches "Deadpool.and.Wolverine.2024..."
        c4 = [{'title': 'Deadpool.and.Wolverine.2024.1080p.WEB-DL.DDP5.1-GRP', 'guid': 'm4', 'nzb_url': 'u', 'size_bytes': 5 * 1024**3}]
        r4 = select_releases(c4, 'movie', {'title': 'Deadpool & Wolverine', 'year': 2024})
        self.assertEqual(len(r4), 1)

    def test_daily_airdate_episode_matching(self):
        p = parse_release('WWE.RAW.2026.11.23.NF.iNT.1080p.WEB-DL.HFR.H.265-PNiX')
        self.assertEqual(p['airdate'], '2026-11-23')
        self.assertEqual(p['resolution'], '1080p')
        self.assertEqual(p['source'], 'webdl')

        candidates = [
            {'title': 'WWE.RAW.2026.11.23.NF.iNT.1080p.WEB-DL.HFR.H.265-PNiX', 'guid': 'raw_ok', 'nzb_url': 'u', 'size_bytes': 3 * 1024**3},
            {'title': 'WWE.RAW.2026.11.16.NF.iNT.1080p.WEB-DL.HFR.H.265-PNiX', 'guid': 'raw_wrong_date', 'nzb_url': 'u', 'size_bytes': 3 * 1024**3},
        ]
        ranked = select_releases(
            candidates, 'episode',
            {'title': 'WWE Raw', 'season': 33, 'episode': 47, 'airdate': '2026-11-23'},
            preferred_resolution='1080p', max_size_gb=8,
        )
        self.assertEqual([r['guid'] for r in ranked], ['raw_ok'])

    def test_multi_episode_and_season_pack_selection(self):
        p_multi = parse_release('Severance.S01E01-E02.1080p.WEB-DL.DDP5.1.H.264-GRP')
        self.assertEqual(p_multi['season'], 1)
        self.assertEqual(p_multi['episode'], 1)
        self.assertEqual(p_multi['ep_end'], 2)

        p_pack = parse_release('Severance.S01.1080p.ATVP.WEB-DL.DDP5.1.H.264-GRP')
        self.assertTrue(p_pack['is_season_pack'])
        self.assertEqual(p_pack['season'], 1)

        candidates = [
            {'title': 'Severance.S01.1080p.ATVP.WEB-DL.DDP5.1.H.264-GRP', 'guid': 'pack_s01', 'nzb_url': 'u', 'size_bytes': 25 * 1024**3},
            {'title': 'Severance.S02.1080p.ATVP.WEB-DL.DDP5.1.H.264-GRP', 'guid': 'pack_s02', 'nzb_url': 'u', 'size_bytes': 25 * 1024**3},
        ]
        # Rejected when allow_season_packs=False
        r_no_pack = select_releases(candidates, 'episode', {'title': 'Severance', 'season': 1, 'episode': 1}, max_size_gb=8)
        self.assertEqual(len(r_no_pack), 0)

        # Accepted (only S01, not S02) when allow_season_packs=True
        r_pack = select_releases(candidates, 'episode', {'title': 'Severance', 'season': 1, 'episode': 1}, max_size_gb=8, allow_season_packs=True)
        self.assertEqual([r['guid'] for r in r_pack], ['pack_s01'])


if __name__ == '__main__':
    unittest.main()
