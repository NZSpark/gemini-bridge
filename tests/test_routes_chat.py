import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import json
from fastapi.testclient import TestClient
from gemini_web.server import app

class ChatRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch('gemini_web.server.driver')
    def test_healthz_endpoint_ready(self, mock_driver):
        mock_driver.page = MagicMock()
        mock_driver.session_stats.return_value = {}
        mock_driver.session_keys.return_value = []
        mock_driver.cluster_stats.return_value = {}
        mock_driver.init_error = None

        res = self.client.get('/healthz')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get('status'), 'ok')
        self.assertIn('cluster', data)

    @patch('gemini_web.server.driver')
    def test_healthz_endpoint_degraded(self, mock_driver):
        mock_driver.page = None
        mock_driver.session_stats.return_value = {}
        mock_driver.session_keys.return_value = []
        mock_driver.cluster_stats.return_value = {}
        mock_driver.init_error = "Browser failed to start"

        res = self.client.get('/healthz')
        self.assertEqual(res.status_code, 503)
        data = res.json()
        self.assertEqual(data.get('status'), 'degraded')
        self.assertEqual(data.get('init_error'), "Browser failed to start")

    def test_models_endpoint(self):
        res = self.client.get('/v1/models')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get('object'), 'list')
        self.assertTrue(len(data.get('data', [])) > 0)

    def test_models_endpoint_exposes_context_window(self):
        """T8.7：/v1/models 必须透出 context_window，且与 SESSION_MAX_TOKENS 同源。"""
        from gemini_web import config

        res = self.client.get('/v1/models')
        self.assertEqual(res.status_code, 200)
        for card in res.json()['data']:
            self.assertEqual(card['context_window'], config.SESSION_MAX_TOKENS)

    def test_context_window_follows_config(self):
        from unittest import mock

        from gemini_web import config

        with mock.patch.object(config, 'SESSION_MAX_TOKENS', 4321):
            res = self.client.get('/v1/models')
        self.assertEqual(res.json()['data'][0]['context_window'], 4321)
