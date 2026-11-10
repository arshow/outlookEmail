import importlib
import os
import tempfile
import unittest
from unittest.mock import patch


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
os.environ.setdefault('DATABASE_PATH', os.path.join(tempfile.mkdtemp(prefix='graph-delete-'), 'test.db'))
web_outlook_app = importlib.import_module('web_outlook_app')


class Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class GraphDeleteErrorTests(unittest.TestCase):
    def test_batch_delete_includes_graph_body_and_reauth_hint(self):
        response = Response(200, {
            'responses': [{
                'id': '0',
                'status': 403,
                'body': {'error': {'code': 'ErrorAccessDenied', 'message': 'Access is denied.'}},
            }],
        })
        with patch.object(web_outlook_app, 'get_access_token_graph', return_value='token'), \
             patch.object(web_outlook_app, 'request_with_proxy_failover', return_value=response):
            result = web_outlook_app.delete_emails_graph('client', 'refresh', ['msg-1'])

        self.assertFalse(result['success'])
        self.assertEqual(result['failed_count'], 1)
        self.assertIn('Mail.ReadWrite', result['error'])
        self.assertIn('重新授权', result['error'])
        self.assertIn('Access is denied', result['error'])
        self.assertEqual(result['errors'][0]['error']['code'], 'EMAIL_DELETE_REAUTH_REQUIRED')


if __name__ == '__main__':
    unittest.main()
