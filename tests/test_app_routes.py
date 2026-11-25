import os
import unittest

os.environ['ROLARR_NO_BACKGROUND'] = '1'

import app as rolarr_app


class TestAppRoutes(unittest.TestCase):
    def setUp(self):
        self.client = rolarr_app.app.test_client()

    def test_index_renders_without_legacy_services(self):
        res = self.client.get('/')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn('NZBgeek', html)
        self.assertIn('Usenet', html)
        self.assertIn('tab-shows', html)
        self.assertIn('tab-movies', html)
        self.assertNotIn('Sonarr Connection', html)
        self.assertNotIn('Radarr Connection', html)
        self.assertNotIn('NZBGet', html)

    def test_api_endpoints_respond(self):
        r_conn = self.client.get('/api/connection-status')
        self.assertEqual(r_conn.status_code, 200)
        data = r_conn.get_json()
        self.assertIn('nzbgeek', data)
        self.assertIn('usenet', data)
        self.assertNotIn('sonarr', data)
        self.assertNotIn('radarr', data)

        r_dl = self.client.get('/api/downloads-status')
        self.assertEqual(r_dl.status_code, 200)
        self.assertIn('downloads', r_dl.get_json())

        r_lib = self.client.get('/api/library')
        self.assertEqual(r_lib.status_code, 200)
        self.assertIn('shows', r_lib.get_json())
        self.assertIn('movies', r_lib.get_json())

    def test_pwa_manifest_and_service_worker(self):
        r_man = self.client.get('/manifest.json')
        self.assertEqual(r_man.status_code, 200)
        manifest = r_man.get_json()
        self.assertEqual(manifest['display'], 'standalone')
        sizes = {icon['sizes'] for icon in manifest['icons']}
        self.assertIn('192x192', sizes)
        self.assertIn('512x512', sizes)

        r_sw = self.client.get('/sw.js')
        self.assertEqual(r_sw.status_code, 200)
        self.assertEqual(r_sw.headers.get('Service-Worker-Allowed'), '/')

    def test_manual_blocklist_release_endpoint(self):
        guid = 'test-manual-blocklist-guid-123'
        title = 'Show.S01E01.1080p.WEB-DL-GRP'

        r_add = self.client.post('/api/releases/blocklist', json={
            'nzb_guid': guid,
            'nzb_title': title,
        })
        self.assertEqual(r_add.status_code, 200)
        self.assertTrue(r_add.get_json()['blocklisted'])
        self.assertIn(guid, rolarr_app.db.get_blocklisted_guids())

        r_del = self.client.delete('/api/releases/blocklist', json={
            'nzb_guid': guid,
            'nzb_title': title,
        })
        self.assertEqual(r_del.status_code, 200)
        self.assertFalse(r_del.get_json()['blocklisted'])
        self.assertNotIn(guid, rolarr_app.db.get_blocklisted_guids())


if __name__ == '__main__':
    unittest.main()
