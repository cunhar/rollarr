import os
import tempfile
import unittest
from unittest.mock import patch

os.environ['ROLARR_NO_BACKGROUND'] = '1'

import app as rolarr_app
from services import db, player_service, plex_watcher


class TestPlayerService(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.orig_db = db.get_db_path()
        db.set_db_path(os.path.join(self.tmpdir.name, 'test_player.db'))
        db.init_db()

        self.tv_dir = os.path.join(self.tmpdir.name, 'tv')
        self.movies_dir = os.path.join(self.tmpdir.name, 'movies')
        os.makedirs(self.tv_dir, exist_ok=True)
        os.makedirs(self.movies_dir, exist_ok=True)

        self.client = rolarr_app.app.test_client()

    def tearDown(self):
        db.set_db_path(self.orig_db)
        self.tmpdir.cleanup()

    def _fake_cfg(self, key, default=None):
        if key == 'PATH_TV':
            return self.tv_dir
        if key == 'PATH_MOVIES':
            return self.movies_dir
        if key == 'DELETE_WATCHED_EPISODES':
            return True
        if key == 'ROLLING_WINDOW':
            return 2
        return default

    def test_srt_to_vtt_conversion(self):
        srt = (
            "1\r\n"
            "00:00:01,250 --> 00:00:04,500\r\n"
            "Hello LG TV Browser!\r\n\r\n"
            "2\n"
            "00:01:10,000 --> 00:01:12,999\n"
            "Zero-CPU remux test.\n"
        )
        vtt = player_service.srt_to_vtt(srt)
        self.assertTrue(vtt.startswith("WEBVTT\n\n"))
        self.assertIn("00:00:01.250 --> 00:00:04.500", vtt)
        self.assertIn("00:01:10.000 --> 00:01:12.999", vtt)
        self.assertIn("Hello LG TV Browser!", vtt)

    def test_player_info_subtitles_and_byte_range_streaming(self):
        show = db.upsert_show(tvmaze_id=999, title='Severance', year=2022)
        ep_dir = os.path.join(self.tv_dir, 'Severance', 'Season 01')
        os.makedirs(ep_dir, exist_ok=True)
        ep_file = os.path.join(ep_dir, 'Severance - S01E01.mp4')
        payload = b'0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ' * 100
        with open(ep_file, 'wb') as f:
            f.write(payload)

        srt_en = os.path.join(ep_dir, 'Severance - S01E01.en.srt')
        with open(srt_en, 'w', encoding='utf-8') as f:
            f.write("1\n00:00:02,100 --> 00:00:05,800\nWelcome to Lumon.\n")

        with patch('services.media_manager.get_config', side_effect=self._fake_cfg), \
             patch('services.player_service.get_config', side_effect=self._fake_cfg), \
             patch('services.media_manager.metadata.get_show_episodes', return_value=[
                 {'season': 1, 'episode': 1, 'title': 'Good News About Hell', 'airdate': '2022-02-18', 'aired': True},
             ]):

            # 1. Info endpoint
            res_info = self.client.get(f"/api/player/info/episode/{show['id']}?season=1&episode=1")
            self.assertEqual(res_info.status_code, 200)
            info = res_info.get_json()
            self.assertEqual(info['status'], 'success')
            self.assertEqual(len(info['subtitles']), 1)
            self.assertEqual(info['subtitles'][0]['id'], 'srt_en')

            # 2. Subtitle VTT endpoint
            res_sub = self.client.get(f"/api/player/subtitles/episode/{show['id']}/srt_en.vtt?season=1&episode=1")
            self.assertEqual(res_sub.status_code, 200)
            vtt_text = res_sub.get_data(as_text=True)
            self.assertIn('WEBVTT', vtt_text)
            self.assertIn('00:00:02.100 --> 00:00:05.800', vtt_text)

            # 3. HTTP 206 Range streaming (forcing direct play mode)
            with patch('services.player_service.probe_media_file', return_value={
                'container': 'mp4',
                'duration_sec': 1200.0,
                'video_codec': 'h264',
                'audio_tracks': [{'index': 0, 'codec': 'aac', 'lang': 'en', 'label': 'English AAC', 'channels': 2}],
                'embedded_subtitles': [],
                'can_direct_play': True,
            }):
                res_stream = self.client.get(
                    f"/api/player/stream/episode/{show['id']}?season=1&episode=1",
                    headers={'Range': 'bytes=10-29'},
                )
                self.assertEqual(res_stream.status_code, 206)
                self.assertEqual(res_stream.data, payload[10:30])
                self.assertIn('bytes 10-29/', res_stream.headers.get('Content-Range', ''))

    def test_heartbeat_active_streams_and_watched_recycling(self):
        mov = db.upsert_movie(tmdb_id=555, title='Dune', year=2021, status='downloaded')
        mov_dir = os.path.join(self.movies_dir, 'Dune (2021)')
        os.makedirs(mov_dir, exist_ok=True)
        mov_file = os.path.join(mov_dir, 'Dune (2021).mkv')
        with open(mov_file, 'wb') as f:
            f.write(b'fake-mkv-video-content')

        with patch('services.media_manager.get_config', side_effect=self._fake_cfg), \
             patch('services.player_service.get_config', side_effect=self._fake_cfg):

            # Send playing heartbeat at 300s / 1000s (30%)
            hb = self.client.post('/api/player/heartbeat', json={
                'media_type': 'movie',
                'media_id': mov['id'],
                'title': 'Dune (2021)',
                'position_sec': 300,
                'duration_sec': 1000,
                'state': 'playing',
                'stream_mode': 'remux',
            })
            self.assertEqual(hb.status_code, 200)
            self.assertAlmostEqual(hb.get_json()['progress_pct'], 30.0)

            # Verify progress saved in SQLite
            prog = db.get_playback_progress('movie', mov['id'])
            self.assertIsNotNone(prog)
            self.assertEqual(prog['position_sec'], 300.0)

            # Verify active stream is reported to Shutdown Watcher
            active = player_service.get_active_streams()
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]['title'], 'Dune (2021)')
            self.assertIn('Rolarr Player', active[0]['device'])

            watcher_state = plex_watcher.get_state(refresh_live=True)
            self.assertGreaterEqual(watcher_state['stream_count'], 1)

            # Mark movie watched -> deletes file & clears active session
            w_res = self.client.post('/api/player/watched', json={
                'media_type': 'movie',
                'media_id': mov['id'],
            })
            self.assertEqual(w_res.status_code, 200)
            self.assertFalse(os.path.exists(mov_file))
            self.assertIsNone(db.get_playback_progress('movie', mov['id']))
            self.assertEqual(len(player_service.get_active_streams()), 0)


if __name__ == '__main__':
    unittest.main()
