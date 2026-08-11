import importlib
import io
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-send-tests-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')
ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

web_outlook_app = importlib.import_module('web_outlook_app')


class EmailSendApiTests(unittest.TestCase):
    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['logged_in'] = True

        with self.app.app_context():
            web_outlook_app.init_db()
            db = web_outlook_app.get_db()
            db.execute('DELETE FROM accounts')
            db.execute("DELETE FROM groups WHERE name NOT IN ('默认分组', '临时邮箱')")
            db.commit()
            self.assertTrue(web_outlook_app.add_account(
                'sender@outlook.com',
                'password',
                'client-id',
                'refresh-token',
                group_id=1,
                account_type='outlook',
                provider='outlook',
            ))
            self.assertTrue(web_outlook_app.add_account(
                'imap-user@qq.com',
                '',
                '',
                '',
                group_id=1,
                account_type='imap',
                provider='qq',
                imap_password='auth-code',
            ))
            self.assertTrue(web_outlook_app.add_account(
                'custom@example.com',
                '',
                '',
                '',
                group_id=1,
                account_type='imap',
                provider='custom',
                imap_host='imap.example.com',
                imap_password='auth-code',
                smtp_host='',
            ))

    def test_oauth_scopes_include_mail_send(self):
        self.assertIn(
            'https://graph.microsoft.com/Mail.Send',
            web_outlook_app.OAUTH_GRAPH_SCOPES,
        )
        self.assertIn(
            'https://graph.microsoft.com/Mail.Send',
            web_outlook_app.OAUTH_SCOPES,
        )

    def test_send_requires_recipients(self):
        response = self.client.post('/api/emails/send', json={
            'email': 'sender@outlook.com',
            'subject': 'hi',
            'body_html': '<p>hello</p>',
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()['success'])

    def test_send_via_graph_success(self):
        with patch.object(web_outlook_app, 'send_email_via_graph', return_value={'success': True}) as mocked:
            response = self.client.post('/api/emails/send', json={
                'email': 'sender@outlook.com',
                'to': ['a@example.com'],
                'subject': 'hello',
                'body_html': '<p>world</p>',
            })
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        mocked.assert_called_once()

    def test_send_graph_missing_scope(self):
        error = web_outlook_app.build_error_payload(
            'GRAPH_MAIL_SEND_SCOPE_REQUIRED',
            '当前账号缺少 Mail.Send 权限，请重新授权后再发信',
            'GraphAPIError',
            403,
        )
        with patch.object(web_outlook_app, 'send_email_via_graph', return_value={'success': False, 'error': error}):
            response = self.client.post('/api/emails/send', json={
                'email': 'sender@outlook.com',
                'to': ['a@example.com'],
                'subject': 'hello',
                'body_text': 'world',
            })
        self.assertEqual(response.status_code, 403)
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertEqual(payload['code'], 'GRAPH_MAIL_SEND_SCOPE_REQUIRED')

    def test_reply_via_graph(self):
        with patch.object(web_outlook_app, 'reply_email_via_graph', return_value={'success': True, 'message_id': 'draft-1'}) as mocked:
            response = self.client.post('/api/emails/reply', json={
                'email': 'sender@outlook.com',
                'message_id': 'AAMk-1',
                'body_html': '<p>thanks</p>',
                'reply_all': False,
            })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])
        mocked.assert_called_once()

    def test_imap_smtp_send_success(self):
        with patch.object(web_outlook_app, 'send_email_via_account_smtp', return_value={'success': True}) as mocked:
            response = self.client.post('/api/emails/send', json={
                'email': 'imap-user@qq.com',
                'to': ['a@example.com'],
                'subject': 'smtp',
                'body_text': 'hello',
            })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])
        mocked.assert_called_once()

    def test_custom_imap_without_smtp_rejected(self):
        with patch.object(
            web_outlook_app,
            'send_email_via_account_smtp',
            return_value={
                'success': False,
                'error': web_outlook_app.build_error_payload(
                    'SMTP_CONFIG_REQUIRED',
                    '当前账号未配置 SMTP，无法发信',
                    'ValidationError',
                    400,
                ),
            },
        ):
            response = self.client.post('/api/emails/send', json={
                'email': 'custom@example.com',
                'to': ['a@example.com'],
                'subject': 'smtp',
                'body_text': 'hello',
            })
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertEqual(payload['code'], 'SMTP_CONFIG_REQUIRED')

    def test_attachment_blocked_extension(self):
        data = {
            'email': 'sender@outlook.com',
            'to': '["a@example.com"]',
            'subject': 'with attach',
            'body_html': '<p>x</p>',
        }
        data_files = {
            'attachments': (io.BytesIO(b'malware'), 'evil.exe'),
        }
        with patch.object(web_outlook_app, 'send_email_via_graph', return_value={'success': True}) as mocked:
            response = self.client.post(
                '/api/emails/send',
                data={**data, **{'attachments': data_files['attachments']}},
                content_type='multipart/form-data',
            )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertEqual(payload['code'], 'EMAIL_ATTACHMENT_BLOCKED')
        mocked.assert_not_called()

    def test_attachment_too_large(self):
        big = b'x' * (web_outlook_app.EMAIL_ATTACHMENT_MAX_BYTES + 1)
        with patch.object(web_outlook_app, 'send_email_via_graph', return_value={'success': True}) as mocked:
            response = self.client.post(
                '/api/emails/send',
                data={
                    'email': 'sender@outlook.com',
                    'to': '["a@example.com"]',
                    'subject': 'big',
                    'body_text': 'x',
                    'attachments': (io.BytesIO(big), 'big.bin'),
                },
                content_type='multipart/form-data',
            )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertEqual(payload['code'], 'EMAIL_ATTACHMENT_TOO_LARGE')
        mocked.assert_not_called()

    def test_resolve_qq_smtp_preset(self):
        with self.app.app_context():
            account = web_outlook_app.get_account_by_email('imap-user@qq.com')
            config = web_outlook_app.resolve_account_smtp_config(account)
        self.assertEqual(config['host'], 'smtp.qq.com')
        self.assertEqual(config['port'], 465)
        self.assertTrue(config['use_ssl'])


if __name__ == '__main__':
    unittest.main()
