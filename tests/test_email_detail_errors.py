import importlib
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-detail-error-tests-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')
ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

web_outlook_app = importlib.import_module('web_outlook_app')


class EmailDetailErrorTests(unittest.TestCase):
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
            db.execute('DELETE FROM retained_normal_mail_messages')
            db.execute('DELETE FROM account_aliases')
            db.execute('DELETE FROM account_tags')
            db.execute('DELETE FROM accounts')
            db.execute("DELETE FROM groups WHERE name NOT IN ('默认分组', '临时邮箱')")
            db.commit()
            added = web_outlook_app.add_account(
                'detail-error@example.com',
                'password',
                'client-id',
                'refresh-token',
                group_id=1,
                account_type='outlook',
                provider='outlook',
            )
            self.assertTrue(added)
            self.account = web_outlook_app.get_account_by_email('detail-error@example.com')

    def _graph_error(self, message='Graph 获取邮件详情失败', code='EMAIL_DETAIL_FETCH_FAILED', status=403):
        return web_outlook_app.build_error_payload(
            code,
            message,
            'GraphAPIError',
            status,
            {'error': {'code': 'ErrorAccessDenied', 'message': message}},
        )

    def _imap_error(self, message='IMAP 获取邮件详情失败', code='EMAIL_DETAIL_FETCH_FAILED', status=502):
        return web_outlook_app.build_error_payload(
            code,
            message,
            'IMAPFetchError',
            status,
            {'status': 'NO', 'message_id': 'msg-1'},
        )

    def test_graph_detail_token_failure_returns_structured_error(self):
        token_error = web_outlook_app.build_error_payload(
            'GRAPH_TOKEN_FAILED',
            '获取访问令牌失败',
            'GraphAPIError',
            400,
            {'error': 'invalid_grant'},
        )
        with patch.object(
            web_outlook_app,
            'get_access_token_graph_result',
            return_value={'success': False, 'error': token_error},
        ) as token_mock, patch.object(
            web_outlook_app,
            'get_email_detail_imap_result',
            return_value={'success': False, 'error': self._imap_error('IMAP fallback failed')},
        ) as imap_mock:
            response = self.client.get(
                '/api/email/detail-error@example.com/msg-1?method=graph&folder=inbox&id_mode=graph'
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertIsInstance(payload['error'], dict)
        self.assertTrue(payload['error'].get('message'))
        self.assertIn('graph', payload.get('details') or {})
        self.assertNotIn('imap_new', payload.get('details') or {})
        self.assertEqual(payload['details']['graph']['code'], 'GRAPH_TOKEN_FAILED')
        self.assertEqual(payload['error']['code'], 'GRAPH_TOKEN_FAILED')
        token_mock.assert_called()
        imap_mock.assert_not_called()

    def test_graph_detail_http_failure_returns_structured_error_without_silent_none(self):
        class FakeResponse:
            status_code = 404

            def json(self):
                return {'error': {'code': 'ErrorItemNotFound', 'message': 'not found'}}

            @property
            def text(self):
                return '{"error":{"code":"ErrorItemNotFound"}}'

            @property
            def reason(self):
                return 'Not Found'

        with patch.object(
            web_outlook_app,
            'get_access_token_graph_result',
            return_value={'success': True, 'access_token': 'access-token'},
        ), patch.object(
            web_outlook_app,
            'get_with_proxy_fallback',
            return_value=FakeResponse(),
        ), patch.object(
            web_outlook_app,
            'get_email_detail_imap_result',
            return_value={'success': False, 'error': self._imap_error('IMAP also failed')},
        ):
            response = self.client.get(
                '/api/email/detail-error@example.com/missing-id?method=graph&folder=inbox'
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertIsInstance(payload['error'], dict)
        self.assertEqual(payload['details']['graph']['code'], 'EMAIL_DETAIL_FETCH_FAILED')
        self.assertEqual(payload['details']['graph']['status'], 404)
        self.assertEqual(payload['details']['imap_new']['message'], 'IMAP also failed')

    def test_graph_failure_imap_success_returns_detail(self):
        email_detail = {
            'id': 'msg-1',
            'subject': 'IMAP recovered',
            'from': 'sender@example.com',
            'to': 'detail-error@example.com',
            'cc': '',
            'date': '2026-07-25T01:00:00Z',
            'body': '<p>body</p>',
            'body_type': 'html',
            'attachments': [],
        }
        with patch.object(
            web_outlook_app,
            'get_email_detail_graph_result',
            return_value={'success': False, 'error': self._graph_error()},
        ) as graph_mock, patch.object(
            web_outlook_app,
            'get_email_detail_imap_result',
            return_value={'success': True, 'email': email_detail},
        ) as imap_mock:
            response = self.client.get(
                '/api/email/detail-error@example.com/msg-1?method=graph&folder=inbox&id_mode=uid'
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['email']['subject'], 'IMAP recovered')
        graph_mock.assert_called_once()
        imap_mock.assert_called_once()

    def test_both_protocols_fail_returns_attempt_details(self):
        with patch.object(
            web_outlook_app,
            'get_email_detail_graph_result',
            return_value={'success': False, 'error': self._graph_error('Graph denied')},
        ), patch.object(
            web_outlook_app,
            'get_email_detail_imap_result',
            return_value={'success': False, 'error': self._imap_error('IMAP timeout')},
        ):
            response = self.client.get(
                '/api/email/detail-error@example.com/msg-1?method=graph&folder=inbox'
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertEqual(payload['error']['message'], 'Graph denied')
        self.assertEqual(payload['details']['graph']['message'], 'Graph denied')
        self.assertEqual(payload['details']['imap_new']['message'], 'IMAP timeout')
        self.assertEqual(set(payload.get('attempted') or []), {'graph', 'imap_new'})

    def test_outlook_account_prefers_graph_even_when_method_is_imap(self):
        graph_detail = {
            'id': 'msg-1',
            'subject': 'Graph recovered',
            'from': {'emailAddress': {'address': 'sender@example.com'}},
            'toRecipients': [{'emailAddress': {'address': 'detail-error@example.com'}}],
            'ccRecipients': [],
            'receivedDateTime': '2026-07-25T01:00:00Z',
            'body': {'content': '<p>body</p>', 'contentType': 'html'},
            'hasAttachments': False,
        }
        with patch.object(
            web_outlook_app,
            'get_email_detail_imap_result',
        ) as imap_mock, patch.object(
            web_outlook_app,
            'get_email_detail_graph_result',
            return_value={'success': True, 'detail': graph_detail},
        ) as graph_mock, patch.object(
            web_outlook_app,
            'get_email_attachments_graph',
            return_value=[],
        ):
            response = self.client.get(
                '/api/email/detail-error@example.com/msg-1?method=imap&folder=inbox&id_mode=uid'
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['email']['subject'], 'Graph recovered')
        graph_mock.assert_called_once()
        imap_mock.assert_not_called()

    def test_outlook_graph_failure_falls_back_to_imap(self):
        with patch.object(
            web_outlook_app,
            'get_email_detail_graph_result',
            return_value={'success': False, 'error': self._graph_error('Graph denied')},
        ) as graph_mock, patch.object(
            web_outlook_app,
            'get_email_detail_imap_result',
            return_value={'success': False, 'error': self._imap_error('IMAP only failed')},
        ) as imap_mock:
            response = self.client.get(
                '/api/email/detail-error@example.com/msg-1?method=imap&folder=inbox&id_mode=uid'
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertEqual(payload['details']['imap_new']['message'], 'IMAP only failed')
        self.assertEqual(payload['details']['graph']['message'], 'Graph denied')
        graph_mock.assert_called_once()
        imap_mock.assert_called_once()

    def test_graph_id_mode_does_not_fall_back_to_imap(self):
        with patch.object(
            web_outlook_app,
            'get_email_detail_graph_result',
            return_value={'success': False, 'error': self._graph_error('Graph denied')},
        ) as graph_mock, patch.object(
            web_outlook_app,
            'get_email_detail_imap_result',
        ) as imap_mock:
            response = self.client.get(
                '/api/email/detail-error@example.com/AAMk-graph-id'
                '?method=graph&folder=graph:folder-1&id_mode=graph'
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertEqual(payload['error']['message'], 'Graph denied')
        self.assertEqual(payload['error']['type'], 'GraphAPIError')
        self.assertNotIn('imap_new', payload.get('details') or {})
        graph_mock.assert_called_once()
        imap_mock.assert_not_called()

    def test_non_microsoft_account_uses_imap_only(self):
        with self.app.app_context():
            added = web_outlook_app.add_account(
                'imap-only@example.com',
                'password',
                '',
                '',
                group_id=1,
                account_type='imap',
                provider='custom',
                imap_host='imap.example.com',
                imap_port=993,
                imap_password='secret',
            )
            self.assertTrue(added)

        with patch.object(
            web_outlook_app,
            'get_email_detail_graph_result',
        ) as graph_mock, patch.object(
            web_outlook_app,
            'get_email_detail_imap_generic_result',
            return_value={'success': False, 'error': self._imap_error('generic imap failed')},
        ) as imap_mock:
            response = self.client.get(
                '/api/email/imap-only@example.com/msg-1?method=graph&folder=inbox'
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertEqual(payload['error']['message'], 'generic imap failed')
        graph_mock.assert_not_called()
        imap_mock.assert_called_once()

    def test_legacy_get_email_detail_graph_still_returns_none_on_failure(self):
        with patch.object(
            web_outlook_app,
            'get_email_detail_graph_result',
            return_value={'success': False, 'error': self._graph_error()},
        ):
            detail = web_outlook_app.get_email_detail_graph(
                'client-id', 'refresh-token', 'msg-1'
            )
        self.assertIsNone(detail)

    def test_legacy_get_email_detail_imap_still_returns_none_on_failure(self):
        with patch.object(
            web_outlook_app,
            'get_email_detail_imap_result',
            return_value={'success': False, 'error': self._imap_error()},
        ):
            detail = web_outlook_app.get_email_detail_imap(
                'detail-error@example.com',
                'client-id',
                'refresh-token',
                'msg-1',
            )
        self.assertIsNone(detail)

    def test_imap_detail_retries_transport_error_once_then_succeeds(self):
        transport_error = web_outlook_app.build_mail_fetch_error(
            TimeoutError('timed out'),
            proxy_url='socks5://127.0.0.1:1080',
            operation='获取邮件详情',
            legacy_code='EMAIL_DETAIL_FETCH_FAILED',
            legacy_message='获取邮件详情失败',
            legacy_status=500,
        )
        email_detail = {
            'id': 'msg-1',
            'subject': 'Recovered after retry',
            'from': 'sender@example.com',
            'to': 'detail-error@example.com',
            'cc': '',
            'date': '2026-07-25T01:00:00Z',
            'body': '<p>body</p>',
            'body_type': 'html',
            'attachments': [],
        }
        with patch.object(
            web_outlook_app,
            '_get_email_detail_imap_result_once',
            side_effect=[
                {'success': False, 'error': transport_error},
                {'success': True, 'email': email_detail},
            ],
        ) as once_mock, patch.object(web_outlook_app.time, 'sleep') as sleep_mock:
            result = web_outlook_app.get_email_detail_imap_result(
                'detail-error@example.com',
                'client-id',
                'refresh-token',
                'msg-1',
            )

        self.assertTrue(result['success'])
        self.assertEqual(result['email']['subject'], 'Recovered after retry')
        self.assertTrue(result.get('retried'))
        self.assertEqual(result.get('attempts'), 2)
        self.assertEqual(once_mock.call_count, 2)
        sleep_mock.assert_called_once()

    def test_imap_detail_does_not_retry_auth_errors(self):
        auth_error = web_outlook_app.build_error_payload(
            'IMAP_AUTH_FAILED',
            'IMAP 认证失败',
            'IMAPAuthError',
            401,
            '',
        )
        with patch.object(
            web_outlook_app,
            '_get_email_detail_imap_result_once',
            return_value={'success': False, 'error': auth_error},
        ) as once_mock, patch.object(web_outlook_app.time, 'sleep') as sleep_mock:
            result = web_outlook_app.get_email_detail_imap_result(
                'detail-error@example.com',
                'client-id',
                'refresh-token',
                'msg-1',
            )

        self.assertFalse(result['success'])
        self.assertEqual(result['error']['code'], 'IMAP_AUTH_FAILED')
        self.assertEqual(result['error'].get('attempts'), 1)
        self.assertFalse(result['error'].get('retried'))
        self.assertEqual(once_mock.call_count, 1)
        sleep_mock.assert_not_called()

    def test_imap_detail_retries_transport_error_then_returns_annotated_failure(self):
        transport_error = web_outlook_app.build_mail_fetch_error(
            ConnectionError('connection refused'),
            proxy_url='',
            operation='获取邮件详情',
            legacy_code='EMAIL_DETAIL_FETCH_FAILED',
            legacy_message='获取邮件详情失败',
            legacy_status=500,
        )
        with patch.object(
            web_outlook_app,
            '_get_email_detail_imap_result_once',
            return_value={'success': False, 'error': transport_error},
        ) as once_mock, patch.object(web_outlook_app.time, 'sleep') as sleep_mock:
            result = web_outlook_app.get_email_detail_imap_result(
                'detail-error@example.com',
                'client-id',
                'refresh-token',
                'msg-1',
            )

        self.assertFalse(result['success'])
        self.assertEqual(result['error'].get('attempts'), 2)
        self.assertTrue(result['error'].get('retried'))
        self.assertTrue(result['error'].get('retryable'))
        self.assertEqual(once_mock.call_count, 2)
        sleep_mock.assert_called_once()


if __name__ == '__main__':
    unittest.main()
