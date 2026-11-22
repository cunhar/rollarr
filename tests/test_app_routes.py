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
        self.assertIn('tab-library', html)
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


if __name__ == '__main__':
    unittest.main()
