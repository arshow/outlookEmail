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

    def test_normalize_compose_message_id_strips_selection_key(self):
        self.assertEqual(
            web_outlook_app.normalize_compose_message_id('12::inbox::graph::AAMkAGI2'),
            'AAMkAGI2',
        )
        self.assertEqual(
            web_outlook_app.normalize_compose_message_id('inbox::graph::AAMkAGI2'),
            'AAMkAGI2',
        )
        self.assertEqual(
            web_outlook_app.normalize_compose_message_id('AAMkAGI2'),
            'AAMkAGI2',
        )

    def test_build_graph_message_action_url_encodes_id(self):
        url = web_outlook_app.build_graph_message_action_url('inbox::graph::AAMk/A+B=', 'createReply')
        self.assertTrue(url.endswith('/createReply'))
        self.assertIn('/me/messages/', url)
        self.assertNotIn('AAMk/A+B=', url)
        self.assertIn('AAMk%2FA%2BB%3D', url)

    def test_reply_normalizes_selection_key_message_id(self):
        with patch.object(
            web_outlook_app,
            'reply_email_via_graph',
            return_value={'success': True, 'message_id': 'draft-1'},
        ) as mocked:
            response = self.client.post('/api/emails/reply', json={
                'email': 'sender@outlook.com',
                'message_id': 'sender@outlook.com::inbox::graph::AAMkRealId',
                'body_html': '<p>hi</p>',
                'reply_all': False,
            })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])
        self.assertEqual(mocked.call_args.kwargs['message_id'], 'AAMkRealId')

    def test_sequence_id_is_not_a_graph_message_id(self):
        self.assertFalse(web_outlook_app.is_graph_provider_message_id('56'))
        self.assertTrue(web_outlook_app.is_graph_provider_message_id(
            'AQMkADAwATMwMAExLWI2NjMtMTgyYy0wMAItMDAKAAAA=='
        ))

    def test_choose_graph_relink_candidate_matches_sender_and_time(self):
        chosen = web_outlook_app.choose_graph_relink_candidate(
            [
                {
                    'id': 'AQMkLIVE=',
                    'receivedDateTime': '2026-10-24T10:35:50Z',
                    'from': {'emailAddress': {'address': 'brendan@gmail.com'}},
                },
                {
                    'id': 'AQMkOLDER=',
                    'receivedDateTime': '2026-10-20T10:35:50Z',
                    'from': {'emailAddress': {'address': 'brendan@gmail.com'}},
                },
            ],
            sender='Brenda Nisbet <brendan@gmail.com>',
            received_at='2026-10-24T10:35:50Z',
            exclude_id='AQMkSTALE=',
        )
        self.assertEqual(chosen, 'AQMkLIVE=')

    def test_relink_finds_live_graph_id_from_retained_metadata(self):
        with self.app.app_context():
            account = web_outlook_app.get_account_by_email('sender@outlook.com')
            db = web_outlook_app.get_db()
            db.execute(
                '''
                INSERT INTO retained_normal_mail_messages (
                    account_id, folder, provider_message_id, id_mode,
                    subject, sender, received_at
                ) VALUES (?, 'junkemail', 'AQMkSTALE=', 'graph', ?, ?, ?)
                ''',
                (
                    account['id'],
                    'Re: Order AM#1004 confirmed',
                    'Brenda Nisbet <brendan@gmail.com>',
                    '2026-10-24T10:35:50Z',
                ),
            )
            db.commit()
            response = MagicMock()
            response.status_code = 200
            response.content = b'{"value":[]}'
            response.json.return_value = {
                'value': [{
                    'id': 'AQMkLIVE=',
                    'receivedDateTime': '2026-10-24T10:35:50Z',
                    'from': {'emailAddress': {'address': 'brendan@gmail.com'}},
                }],
            }
            with patch.object(web_outlook_app, 'get_with_proxy_fallback', return_value=response) as requested:
                found = web_outlook_app.relink_graph_compose_message_id(
                    account,
                    'token',
                    'AQMkSTALE=',
                )
        self.assertEqual(found, 'AQMkLIVE=')
        self.assertIn("subject eq 'Re: Order AM#1004 confirmed'", requested.call_args.kwargs['params']['$filter'])

    def test_create_reply_retries_with_relinked_id_after_item_not_found(self):
        calls = []

        def create(_token, **kwargs):
            calls.append(kwargs['message_id'])
            if kwargs['message_id'] == 'AQMkSTALE=':
                return {
                    'success': False,
                    'error': {
                        'code': 'GRAPH_CREATE_DRAFT_FAILED',
                        'details': '{"error": {"code": "ErrorItemNotFound"}}',
                    },
                }
            return {'success': True, 'message_id': 'draft-1'}

        with self.app.app_context(), patch.object(
            web_outlook_app, '_graph_create_and_send', side_effect=create
        ), patch.object(
            web_outlook_app, 'relink_graph_compose_message_id', return_value='AQMkLIVE='
        ):
            result, used_id = web_outlook_app._create_graph_draft_with_relink(
                {'id': 1},
                'token',
                message_id='AQMkSTALE=',
                action='createReply',
                body_html='<p>hi</p>',
                body_text='',
                to_list=['a@example.com'],
                cc_list=None,
                bcc_list=None,
                subject=None,
                attachments=[],
            )
        self.assertTrue(result['success'])
        self.assertEqual(calls, ['AQMkSTALE=', 'AQMkLIVE='])
        self.assertEqual(used_id, 'AQMkLIVE=')

    def test_create_reply_relinks_imap_sequence_before_calling_graph(self):
        with self.app.app_context(), patch.object(
            web_outlook_app, 'relink_graph_compose_message_id', return_value='AQMkLIVE='
        ) as relink, patch.object(
            web_outlook_app, '_graph_create_and_send', return_value={'success': True, 'message_id': 'draft-1'}
        ) as create:
            result, used_id = web_outlook_app._create_graph_draft_with_relink(
                {'id': 1},
                'token',
                message_id='56',
                action='createReply',
                body_html='<p>hi</p>',
                body_text='',
                to_list=['a@example.com'],
                cc_list=None,
                bcc_list=None,
                subject=None,
                attachments=[],
            )
        self.assertTrue(result['success'])
        self.assertEqual(used_id, 'AQMkLIVE=')
        relink.assert_called_once()
        self.assertEqual(create.call_args.kwargs['message_id'], 'AQMkLIVE=')


if __name__ == '__main__':
    unittest.main()
