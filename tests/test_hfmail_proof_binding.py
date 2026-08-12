import importlib
import os
import tempfile
import unittest
from unittest.mock import patch


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-hfmail-tests-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in os.sys.path:
    os.sys.path.insert(0, ROOT_DIR)

web_outlook_app = importlib.import_module('web_outlook_app')


class FakeResponse:
    def __init__(self, url='', text='', status_code=200, headers=None, payload=None):
        self.url = url
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError('No JSON payload')
        return self._payload


class FakeSession:
    def __init__(self, get_responses=None, post_responses=None):
        self.get_responses = list(get_responses or [])
        self.post_responses = list(post_responses or [])
        self.headers = {}
        self.trust_env = False
        self.proxies = {}
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if not self.get_responses:
            raise AssertionError(f'Unexpected GET {url}')
        return self.get_responses.pop(0)

    def post(self, url, data=None, **kwargs):
        self.post_calls.append((url, data or {}, kwargs))
        if not self.post_responses:
            raise AssertionError(f'Unexpected POST {url}')
        return self.post_responses.pop(0)


class HfmailSettingsTests(unittest.TestCase):
    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['logged_in'] = True
        with self.app.app_context():
            web_outlook_app.init_db()
            web_outlook_app.set_setting('hfmail_base_url', '')
            web_outlook_app.set_setting('hfmail_api_token', '')

    def test_settings_hfmail_roundtrip(self):
        response = self.client.put('/api/settings', json={
            'hfmail_base_url': 'http://127.0.0.1:3000/',
            'hfmail_api_token': 'token-abc',
        })
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])

        with self.app.app_context():
            self.assertEqual(web_outlook_app.get_hfmail_base_url(), 'http://127.0.0.1:3000')
            self.assertEqual(web_outlook_app.get_hfmail_api_token(), 'token-abc')
            self.assertTrue(web_outlook_app.is_hfmail_configured())

        refreshed = self.client.get('/api/settings')
        self.assertEqual(refreshed.status_code, 200)
        settings = refreshed.get_json()['settings']
        self.assertEqual(settings['hfmail_base_url'], 'http://127.0.0.1:3000')
        self.assertEqual(settings['hfmail_api_token'], 'token-abc')


class HfmailClientTests(unittest.TestCase):
    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        with self.app.app_context():
            web_outlook_app.init_db()
            web_outlook_app.set_setting('hfmail_base_url', 'http://hfmail.test')
            web_outlook_app.set_setting('hfmail_api_token', 'bearer-token')

    def test_hfmail_create_mailbox(self):
        with self.app.app_context():
            with patch.object(web_outlook_app, 'hfmail_request') as mocked:
                mocked.return_value = {
                    'success': True,
                    'email': 'userab12cd@hfmail.cc',
                    'status_code': 200,
                }
                email_addr, error = web_outlook_app.hfmail_create_mailbox('user@hotmail.com')
        self.assertEqual(email_addr, 'userab12cd@hfmail.cc')
        self.assertEqual(error, '')
        mocked.assert_called_once()
        args, kwargs = mocked.call_args
        self.assertEqual(args[0], 'POST')
        self.assertEqual(args[1], '/api/v1/mailboxes')
        self.assertEqual(kwargs['json_data'], {'seed': 'user@hotmail.com'})

    def test_hfmail_wait_verification_code(self):
        with self.app.app_context():
            with patch.object(web_outlook_app, 'hfmail_request') as mocked:
                mocked.return_value = {
                    'success': True,
                    'code': '123456',
                    'status_code': 200,
                }
                code, error, payload = web_outlook_app.hfmail_wait_verification_code(
                    'userab12cd@hfmail.cc',
                    since='2026-08-12T01:00:00+00:00',
                    total_timeout_seconds=5,
                    wait_seconds_per_call=1,
                )
        self.assertEqual(code, '123456')
        self.assertEqual(error, '')
        self.assertEqual(payload['code'], '123456')
        mocked.assert_called()
        _args, kwargs = mocked.call_args
        self.assertEqual(kwargs['params']['since'], '2026-08-12T01:00:00+00:00')
        self.assertEqual(kwargs['params']['sender_contains'], 'microsoft')


class GraphOauthProofBindTests(unittest.TestCase):
    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        with self.app.app_context():
            web_outlook_app.init_db()
            web_outlook_app.set_setting('hfmail_base_url', 'http://hfmail.test')
            web_outlook_app.set_setting('hfmail_api_token', 'bearer-token')

    def test_proofs_bind_with_hfmail_then_exchanges_code(self):
        auth_html = '''
            <input name="PPFT" value="flow">
            <script>"urlPost":"https://login.live.com/post.srf","sCtx":"ctx"</script>
        '''
        proofs_html = '''
            <form action="/proofs/Add">
              <input type="hidden" name="canary" value="proof-canary">
              <input type="email" name="EmailAddress" value="">
            </form>
        '''
        verify_html = '''
            <form action="/proofs/Verify">
              <input type="hidden" name="canary" value="verify-canary">
              <input type="text" name="otc" value="">
              <p>Enter your security code</p>
            </form>
        '''
        session = FakeSession(
            get_responses=[
                FakeResponse(url='https://login.microsoftonline.com/auth', text=auth_html),
            ],
            post_responses=[
                FakeResponse(url='https://account.live.com/proofs/Add', text=proofs_html),
                FakeResponse(url='https://account.live.com/proofs/Verify', text=verify_html),
                FakeResponse(status_code=302, headers={'Location': 'http://localhost?code=bound-code'}),
                FakeResponse(payload={'access_token': 'access', 'refresh_token': 'rt-bound'}),
            ],
        )

        with self.app.app_context():
            with patch.object(web_outlook_app, 'hfmail_create_mailbox', return_value=('userab12cd@hfmail.cc', '')), \
                 patch.object(web_outlook_app, 'hfmail_wait_verification_code', return_value=('654321', '', {'code': '654321'})), \
                 patch.object(web_outlook_app, 'hfmail_utc_now_iso', return_value='2026-08-12T01:00:00+00:00'):
                result = web_outlook_app.extract_graph_refresh_token(
                    'user@hotmail.com',
                    'password',
                    session_factory=lambda: session,
                )

        self.assertTrue(result['success'], result)
        self.assertEqual(result['refresh_token'], 'rt-bound')
        # login post, add proof post, verify post, token post
        self.assertEqual(session.post_calls[1][0], 'https://account.live.com/proofs/Add')
        self.assertEqual(session.post_calls[1][1]['EmailAddress'], 'userab12cd@hfmail.cc')
        self.assertEqual(session.post_calls[1][1]['action'], 'AddProof')
        self.assertEqual(session.post_calls[2][0], 'https://account.live.com/proofs/Verify')
        self.assertEqual(session.post_calls[2][1]['otc'], '654321')
        self.assertNotEqual(session.post_calls[1][1].get('action'), 'Skip')

    def test_proofs_falls_back_to_skip_when_hfmail_create_fails(self):
        auth_html = '<input name="PPFT" value="flow"><script>"urlPost":"https://post"</script>'
        proofs_html = '''
            <form action="/proofs/Add">
              <input type="hidden" name="canary" value="proof-canary">
              <input type="email" name="EmailAddress" value="">
            </form>
        '''
        session = FakeSession(
            get_responses=[FakeResponse(url='https://auth', text=auth_html)],
            post_responses=[
                FakeResponse(url='https://account.live.com/proofs/Add', text=proofs_html),
                FakeResponse(status_code=302, headers={'Location': 'http://localhost?code=skip-code'}),
                FakeResponse(payload={'access_token': 'access', 'refresh_token': 'rt-skip'}),
            ],
        )

        with self.app.app_context():
            with patch.object(web_outlook_app, 'hfmail_create_mailbox', return_value=(None, 'down')):
                result = web_outlook_app.extract_graph_refresh_token(
                    'user@hotmail.com',
                    'password',
                    session_factory=lambda: session,
                )

        self.assertTrue(result['success'], result)
        self.assertEqual(result['refresh_token'], 'rt-skip')
        self.assertEqual(session.post_calls[1][1]['action'], 'Skip')


if __name__ == '__main__':
    unittest.main()
