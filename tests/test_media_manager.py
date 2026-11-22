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
        ok, _ = db.move_download(dl2['id'], 'up')
        self.assertTrue(ok)
        self.assertEqual(db.get_next_queued_download()['id'], dl2['id'])
        ok, _ = db.move_download(dl2['id'], 'down')
        self.assertTrue(ok)
        self.assertEqual(db.get_next_queued_download()['id'], dl['id'])

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

            ok_m, _ = media_manager.delete_movie_file('Inception', 2010)
            self.assertTrue(ok_m)
            self.assertFalse(os.path.exists(mov_file))
            self.assertFalse(os.path.exists(mov_dir))  # empty movie folder cleaned up


if __name__ == '__main__':
    unittest.main()
