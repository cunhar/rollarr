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

    def test_watched_episode_never_upgrades_and_cancels_active_downloads(self):
        from services import plex_poller, usenet_downloader

        show = db.upsert_show(tvmaze_id=777, title="The Pastor's Wife", tvdb_id=888, year=2024)
        ep_dir = os.path.join(self.tv_dir, "The Pastor's Wife", 'Season 01')
        os.makedirs(ep_dir, exist_ok=True)
        ep1_path = os.path.join(ep_dir, "The Pastor's Wife - S01E01.720p.mkv")
        ep2_path = os.path.join(ep_dir, "The Pastor's Wife - S01E02.720p.mkv")
        for p in (ep1_path, ep2_path):
            with open(p, 'wb') as f:
                f.write(b'video')

        def fake_cfg(key, default=None):
            if key == 'PATH_TV':
                return self.tv_dir
            if key == 'PATH_MOVIES':
                return self.movies_dir
            if key == 'PREFERRED_RESOLUTION':
                return '1080p'
            if key == 'DELETE_WATCHED_EPISODES':
                return True
            return default

        # 1. Mark S01E01 as watched via watch cursor while file is still on disk -> _try_upgrade_episode must refuse
        db.update_show_watch_cursor(show['id'], 1, 1)
        with patch('services.media_manager.get_config', side_effect=fake_cfg), \
             patch('services.media_manager.nzbgeek.search_episode') as mock_search:
            ok_up, _ = media_manager._try_upgrade_episode(show, 1, 1, '720p', '1080p')
            self.assertFalse(ok_up)
            mock_search.assert_not_called()

        # 2. If an upgrade download was already queued for S01E02 before poller runs,
        #    processing S01E02 as watched cancels the queued upgrade & deletes the file
        dl_upgrade = db.enqueue_download(
            media_type='episode', media_id=show['id'], title="The Pastor's Wife",
            season=1, episode=2, nzb_title='The.Pastors.Wife.S01E02.1080p.WEB-DL',
            nzb_url='http://nzb/up2', nzb_guid='guid-up2', size_bytes=1024,
        )
        self.assertTrue(db.has_active_download('episode', "The Pastor's Wife", 1, 2))

        watched_item = {
            '_source': 'jellyfin',
            'type': 'episode',
            'grandparentTitle': "Pastor's Wife",  # Even without leading 'The'
            'parentIndex': 1,
            'index': 2,
            'title': 'Episode 2',
        }
        with patch('services.media_manager.get_config', side_effect=fake_cfg), \
             patch('services.plex_poller.get_config', side_effect=fake_cfg), \
             patch('services.media_manager.metadata.get_show_episodes', return_value=[]):
            status, _ = plex_poller._process_episode(watched_item)

        self.assertEqual(status, 'success')
        self.assertFalse(os.path.exists(ep2_path))
        self.assertFalse(db.has_active_download('episode', "The Pastor's Wife", 1, 2))
        self.assertEqual(db.get_download(dl_upgrade['id'])['status'], 'cancelled')

        # 3. Downloader _is_job_obsolete_or_cancelled rejects jobs for watched episodes or deleted shows
        obsolete, reason = usenet_downloader._is_job_obsolete_or_cancelled(dl_upgrade)
        self.assertTrue(obsolete)
        self.assertIn('watched', reason)

        db.delete_show(show['id'])
        dl_deleted_show = {
            'id': 9999,
            'media_type': 'episode',
            'media_id': show['id'],
            'title': "The Pastor's Wife",
            'season': 1,
            'episode': 3,
        }
        obsolete_del, reason_del = usenet_downloader._is_job_obsolete_or_cancelled(dl_deleted_show)
        self.assertTrue(obsolete_del)
        self.assertIn('deleted', reason_del)

    def test_plex_watchlist_auto_import_and_removal(self):
        from services import plex_poller

        watchlist_payload = [
            {
                'ratingKey': 'wl-movie-1',
                'type': 'movie',
                'title': 'Dune: Part Two',
                'year': 2024,
                'thumb': 'https://image.tmdb.org/poster.jpg',
                'Guid': [{'id': 'imdb://tt15239678'}, {'id': 'tmdb://693134'}],
            },
            {
                'ratingKey': 'wl-show-1',
                'type': 'show',
                'title': 'Severance',
                'year': 2022,
                'Guid': [{'id': 'imdb://tt11280740'}, {'id': 'tvdb://371980'}],
            },
        ]

        fake_tvmaze_show = {
            'type': 'tv',
            'id': 44933,
            'tvmaze_id': 44933,
            'tvdb_id': 371980,
            'imdb_id': 'tt11280740',
            'title': 'Severance',
            'year': 2022,
            'poster_url': 'https://tvmaze/sev.jpg',
        }
        fake_episodes = [
            {'season': 1, 'episode': 1, 'title': 'Ep 1', 'airdate': '2022-02-18', 'aired': True},
            {'season': 1, 'episode': 2, 'title': 'Ep 2', 'airdate': '2022-02-25', 'aired': True},
        ]

        removed_keys = []

        def fake_remove(rk):
            removed_keys.append(rk)
            return True

        with patch('services.plex_poller.get_config', return_value=True), \
             patch('services.plex_poller.get_plex_credentials', return_value=('http://plex:32400', 'tok-123')), \
             patch('services.plex_poller._fetch_plex_watchlist', return_value=watchlist_payload), \
             patch('services.plex_poller._remove_from_plex_watchlist', side_effect=fake_remove), \
             patch('services.media_manager.metadata.get_movie_details', return_value=None), \
             patch('services.media_manager.metadata.get_movie_release_dates', return_value={'in_cinemas': '2024-03-01', 'digital_release': '2024-04-15'}), \
             patch('services.media_manager.queue_movie', return_value=(True, 'Queued Dune.Part.Two.1080p')), \
             patch('services.plex_poller.metadata.find_show_by_external_id', return_value=fake_tvmaze_show), \
             patch('services.media_manager.metadata.get_show_by_id', return_value=fake_tvmaze_show), \
             patch('services.media_manager.metadata.get_show_episodes', return_value=fake_episodes), \
             patch('services.media_manager.queue_episode', return_value=(True, 'Queued Severance S01E01')):
            imported = plex_poller.sync_plex_watchlist()

        self.assertEqual(imported, 2)
        self.assertEqual(removed_keys, ['wl-movie-1', 'wl-show-1'])
        self.assertEqual(len(db.list_movies()), 1)
        self.assertEqual(db.list_movies()[0]['title'], 'Dune: Part Two')
        self.assertEqual(len(db.list_shows()), 1)
        self.assertEqual(db.list_shows()[0]['title'], 'Severance')


if __name__ == '__main__':
    unittest.main()
