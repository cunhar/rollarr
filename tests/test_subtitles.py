import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from services import subtitle_service


class TestSubtitles(unittest.TestCase):
    def test_parse_languages(self):
        self.assertEqual(subtitle_service.parse_subtitle_languages('en, pt'), ['en', 'pt'])
        self.assertEqual(subtitle_service.parse_subtitle_languages('EN, pt-br , es, en'), ['en', 'pt-br', 'es'])

    def test_pick_best_sub_prefers_por_over_pob(self):
        subs = [
            {'lang': 'pob', 'url': 'https://subs/br.srt'},
            {'lang': 'por', 'url': 'https://subs/pt.srt'},
            {'lang': 'eng', 'url': 'https://subs/en.srt'},
        ]
        pt_match = subtitle_service._pick_best_sub(subs, 'pt')
        self.assertEqual(pt_match['lang'], 'por')
        self.assertEqual(pt_match['url'], 'https://subs/pt.srt')

        br_only = [{'lang': 'pob', 'url': 'https://subs/br.srt'}]
        self.assertEqual(subtitle_service._pick_best_sub(br_only, 'pt')['lang'], 'pob')

    def test_download_subtitles_writes_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            vid = os.path.join(tmp, 'Severance - S02E01.mkv')
            with open(vid, 'wb') as f:
                f.write(b'0' * 1024)

            api_resp = MagicMock()
            api_resp.json.return_value = {
                'subtitles': [
                    {'lang': 'eng', 'url': 'https://subs/en.srt'},
                    {'lang': 'por', 'url': 'https://subs/pt.srt'},
                ]
            }
            srt_resp = MagicMock()
            srt_resp.content = b"1\n00:00:01,000 --> 00:00:04,000\nHello world subtitle line here!\n"

            with patch('services.subtitle_service.requests.get', side_effect=[api_resp, srt_resp, srt_resp]):
                got = subtitle_service.download_subtitles_for_media(
                    video_path=vid, media_type='episode', imdb_id='tt11280740',
                    season=2, episode=1, languages=['en', 'pt'],
                )

            self.assertEqual(got, ['en', 'pt'])
            self.assertTrue(os.path.exists(os.path.join(tmp, 'Severance - S02E01.en.srt')))
            self.assertTrue(os.path.exists(os.path.join(tmp, 'Severance - S02E01.pt.srt')))


if __name__ == '__main__':
    unittest.main()
