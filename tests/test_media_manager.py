import os
import tempfile
import unittest
from unittest.mock import patch

from integrations.common import sanitize_filename, is_safe_subpath
from services import db, media_manager


class TestMediaManagerAndDb(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.orig_db = db.get_db_path()
        db.set_db_path(os.path.join(self.tmpdir.name, 'test_rolarr.db'))
        db.init_db()

        self.tv_dir = os.path.join(self.tmpdir.name, 'tv')
        self.movies_dir = os.path.join(self.tmpdir.name, 'movies')
        os.makedirs(self.tv_dir, exist_ok=True)
        os.makedirs(self.movies_dir, exist_ok=True)

    def tearDown(self):
        db.set_db_path(self.orig_db)
        self.tmpdir.cleanup()

    def test_sanitize_and_safe_subpath(self):
        self.assertEqual(sanitize_filename('Show: The / Return?*'), 'Show The Return')
        inside = os.path.join(self.tv_dir, 'Show', 'ep.mkv')
        outside = os.path.join(self.tmpdir.name, 'outside.mkv')
        self.assertTrue(is_safe_subpath(inside, self.tv_dir))
        self.assertFalse(is_safe_subpath(outside, self.tv_dir))
        self.assertFalse(is_safe_subpath(self.tv_dir, self.tv_dir))

    def test_db_crud_and_blocklist(self):
        show = db.upsert_show(tvmaze_id=123, title='Severance', tvdb_id=456, year=2022)
        self.assertEqual(show['title'], 'Severance')
        self.assertEqual(len(db.list_shows()), 1)

        dl = db.enqueue_download(
            media_type='episode', media_id=show['id'], title='Severance',
            season=1, episode=1, nzb_title='Severance.S01E01.1080p',
            nzb_url='http://nzb/1', nzb_guid='guid-1', size_bytes=1024,
        )
        self.assertTrue(db.has_active_download('episode', 'Severance', 1, 1))
        self.assertEqual(db.get_next_queued_download()['id'], dl['id'])

        dl2 = db.enqueue_download(
            media_type='episode', media_id=show['id'], title='Severance',
            season=1, episode=2, nzb_title='Severance.S01E02.1080p',
            nzb_url='http://nzb/2', nzb_guid='guid-2', size_bytes=1024,
        )
        self.assertTrue(db.move_download(dl2['id'], 'up'))
        self.assertEqual(db.get_next_queued_download()['id'], dl2['id'])
        self.assertTrue(db.move_download(dl2['id'], 'down'))
        self.assertEqual(db.get_next_queued_download()['id'], dl['id'])

        # Simulate dl currently 'downloading' and moving dl2 'up' into #1 slot
        db.update_download(dl['id'], status='downloading', progress_pct=50.0)
        ok_preempt, preempted_id = db.move_download_with_preempt(dl2['id'], 'up')
        self.assertTrue(ok_preempt)
        self.assertEqual(preempted_id, dl['id'])
        self.assertEqual(db.get_next_queued_download()['id'], dl2['id'])

        db.add_to_blocklist('guid-1', 'Severance.S01E01.1080p', 'missing articles')
        self.assertIn('guid-1', db.get_blocklisted_guids())

    def test_direct_episode_and_movie_deletion(self):
        ep_dir = os.path.join(self.tv_dir, 'Severance', 'Season 01')
        os.makedirs(ep_dir, exist_ok=True)
        ep_file = os.path.join(ep_dir, 'Severance - S01E01.mkv')
        with open(ep_file, 'wb') as f:
            f.write(b'video')

        mov_dir = os.path.join(self.movies_dir, 'Inception (2010)')
        os.makedirs(mov_dir, exist_ok=True)
        mov_file = os.path.join(mov_dir, 'Inception (2010).mkv')
        with open(mov_file, 'wb') as f:
            f.write(b'movie')

        def fake_cfg(key, default=None):
            if key == 'PATH_TV':
                return self.tv_dir
            if key == 'PATH_MOVIES':
                return self.movies_dir
            return default

        with patch('services.media_manager.get_config', side_effect=fake_cfg):
            self.assertTrue(media_manager.episode_exists_on_disk('Severance', 1, 1))
            ok, _ = media_manager.delete_episode_file('Severance', 1, 1)
            self.assertTrue(ok)
            self.assertFalse(os.path.exists(ep_file))
            self.assertFalse(os.path.exists(ep_dir))  # empty season folder cleaned up

            mov_dir = os.path.join(self.movies_dir, 'Inception (2010)')
            os.makedirs(mov_dir, exist_ok=True)
            mov_file = os.path.join(mov_dir, 'Inception (2010).mkv')
            mov_vtt = os.path.join(mov_dir, 'Inception (2010)._emb_0.vtt')
            with open(mov_file, 'wb') as f:
                f.write(b'movie')
            with open(mov_vtt, 'wb') as f:
                f.write(b'WEBVTT')

            ok_m, _ = media_manager.delete_movie_file('Inception', 2010)
            self.assertTrue(ok_m)
            self.assertFalse(os.path.exists(mov_file))
            self.assertFalse(os.path.exists(mov_vtt))
            self.assertFalse(os.path.exists(mov_dir))  # movie folder + .vtt sidecar cleaned up

            # Test sync_on_disk_movies auto-imports untracked movie folders and removes empty ones
            orphan_dir = os.path.join(self.movies_dir, 'Nope (2022)')
            os.makedirs(orphan_dir, exist_ok=True)
            orphan_mkv = os.path.join(orphan_dir, 'Nope.2022.2160p.mkv')
            with open(orphan_mkv, 'wb') as f:
                f.write(b'orphan movie')
            empty_dir = os.path.join(self.movies_dir, 'Empty Ghost (2026)')
            os.makedirs(empty_dir, exist_ok=True)

            synced = media_manager.sync_on_disk_movies()
            self.assertFalse(os.path.exists(empty_dir))
            titles = [m['title'] for m in synced]
            self.assertIn('Nope', titles)

            # Test sync_on_disk_shows auto-removes empty show folders and imports untracked show folders
            empty_show = os.path.join(self.tv_dir, 'Empty Ghost Show')
            os.makedirs(empty_show, exist_ok=True)
            orphan_show = os.path.join(self.tv_dir, 'Orphan Show', 'Season 01')
            os.makedirs(orphan_show, exist_ok=True)
            with open(os.path.join(orphan_show, 'Orphan Show - S01E01.mkv'), 'wb') as f:
                f.write(b'ep video')

            with patch('services.media_manager.metadata.find_show_by_title', return_value=None):
                synced_shows = media_manager.sync_on_disk_shows()
            self.assertFalse(os.path.exists(empty_show))
            show_titles = [s['title'] for s in synced_shows]
            self.assertIn('Orphan Show', show_titles)

            # Test sweep_orphaned_temp_dirs
            from services import usenet_downloader
            dl_dir = os.path.join(self.tmpdir.name, 'downloads')
            stale_job_dir = os.path.join(dl_dir, 'tmp', 'job_9999')
            os.makedirs(stale_job_dir, exist_ok=True)
            with patch('services.usenet_downloader.get_config', return_value=dl_dir):
                removed = usenet_downloader.sweep_orphaned_temp_dirs()
            self.assertEqual(removed, 1)
            self.assertFalse(os.path.exists(stale_job_dir))

            # Test manual_delete_episode & manual_delete_season
            show = db.upsert_show(tvmaze_id=123, title='Severance', tvdb_id=456, year=2022)
            os.makedirs(ep_dir, exist_ok=True)
            ep1 = os.path.join(ep_dir, 'Severance - S01E01.mkv')
            ep2 = os.path.join(ep_dir, 'Severance - S01E02.mkv')
            ep3 = os.path.join(ep_dir, 'Severance - S01E03.mkv')
            for p in (ep1, ep2, ep3):
                with open(p, 'wb') as f:
                    f.write(b'video')

            res_ep = media_manager.manual_delete_episode(show['id'], 1, 1)
            self.assertEqual(res_ep['status'], 'success')
            self.assertFalse(os.path.exists(ep1))
            self.assertTrue(os.path.exists(ep2))

            res_sec = media_manager.manual_delete_season(show['id'], 1)
            self.assertEqual(res_sec['status'], 'success')
            self.assertEqual(res_sec['deleted_count'], 2)
            self.assertFalse(os.path.exists(ep2))
            self.assertFalse(os.path.exists(ep3))

            # Test multi-episode file scanning (S01E01-E02 covers both E01 and E02)
            os.makedirs(ep_dir, exist_ok=True)
            multi_ep = os.path.join(ep_dir, 'Severance - S01E01-E02.mkv')
            with open(multi_ep, 'wb') as f:
                f.write(b'multi video')
            self.assertTrue(media_manager.episode_exists_on_disk('Severance', 1, 1))
            self.assertTrue(media_manager.episode_exists_on_disk('Severance', 1, 2))
            self.assertEqual(media_manager._scan_show_episodes_on_disk('Severance'), {(1, 1), (1, 2)})

            # Test movie release availability pre-release guard
            unrel_movie = db.upsert_movie(
                tmdb_id=9991, title='Future Film', year=2027,
                status='wanted', in_cinemas='2027-06-15',
            )
            avail = media_manager.get_movie_availability(unrel_movie, fetch_missing=False)
            self.assertFalse(avail['available'])
            self.assertIn('In Cinemas', avail['label'])


if __name__ == '__main__':
    unittest.main()
