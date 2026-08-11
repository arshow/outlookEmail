import importlib
import os
import sys
import tempfile
import unittest

from unittest.mock import patch


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-flag-tests-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')
ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

web_outlook_app = importlib.import_module('web_outlook_app')


class EmailFlagTests(unittest.TestCase):
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
                'flagged@example.com',
                'password',
                'client-id',
                'refresh-token',
                group_id=1,
                account_type='outlook',
                provider='outlook',
            )
            self.assertTrue(added)
            self.account = web_outlook_app.get_account_by_email('flagged@example.com')

    def _seed_graph_retained_row(self, message_id='flag-graph-1', is_flagged=0):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute(
                '''
                INSERT INTO retained_normal_mail_messages (
                    account_id, folder, provider_message_id, id_mode,
                    subject, sender, recipients, received_at, is_read, is_flagged, list_cached
                )
                VALUES (?, 'inbox', ?, 'graph',
                        'Flag subject', 'sender@example.com',
                        'reader@example.com', '2026-05-27T07:00:00Z', 0, ?, 1)
                ''',
                (self.account['id'], message_id, is_flagged)
            )
            db.commit()

    def _retained_flag_state(self, message_id):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            row = db.execute(
                '''
                SELECT is_flagged, updated_at
                FROM retained_normal_mail_messages
                WHERE account_id = ? AND folder = 'inbox'
                  AND provider_message_id = ? AND id_mode = 'graph'
                ''',
                (self.account['id'], message_id)
            ).fetchone()
        return dict(row)

    def test_coerce_graph_is_flagged(self):
        self.assertTrue(web_outlook_app.coerce_graph_is_flagged({'flagStatus': 'flagged'}))
        self.assertTrue(web_outlook_app.coerce_graph_is_flagged({'flagStatus': 'Flagged'}))
        self.assertFalse(web_outlook_app.coerce_graph_is_flagged({'flagStatus': 'notFlagged'}))
        self.assertFalse(web_outlook_app.coerce_graph_is_flagged({'flagStatus': 'complete'}))
        self.assertFalse(web_outlook_app.coerce_graph_is_flagged(None))
        self.assertFalse(web_outlook_app.coerce_graph_is_flagged('flagged'))

    def test_format_graph_email_item_includes_is_flagged(self):
        flagged_item = web_outlook_app.format_graph_email_item({
            'id': 'AAMk-flagged',
            'subject': 'Starred',
            'from': {'emailAddress': {'address': 'a@example.com'}},
            'toRecipients': [],
            'receivedDateTime': '2026-08-11T10:00:00Z',
            'isRead': True,
            'flag': {'flagStatus': 'flagged'},
            'hasAttachments': False,
            'bodyPreview': 'preview',
        }, 'inbox')
        unflagged_item = web_outlook_app.format_graph_email_item({
            'id': 'AAMk-plain',
            'subject': 'Plain',
            'from': {'emailAddress': {'address': 'b@example.com'}},
            'toRecipients': [],
            'receivedDateTime': '2026-08-11T11:00:00Z',
            'isRead': False,
            'flag': {'flagStatus': 'notFlagged'},
            'hasAttachments': False,
            'bodyPreview': 'preview',
        }, 'inbox')

        self.assertTrue(flagged_item['is_flagged'])
        self.assertFalse(unflagged_item['is_flagged'])
        self.assertEqual(flagged_item['id_mode'], 'graph')

    def test_mark_flag_requires_params(self):
        response = self.client.post('/api/emails/mark-flag', json={
            'email': 'flagged@example.com',
            'flagged': True,
        })
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertEqual(payload['error'], '参数不完整')

    def test_mark_flag_updates_successful_retained_graph_row(self):
        self._seed_graph_retained_row('flag-graph-1', is_flagged=0)
        remote_result = {
            'success': True,
            'success_count': 1,
            'failed_count': 0,
            'updated_ids': ['flag-graph-1'],
            'errors': [],
        }

        with patch.object(web_outlook_app, 'mark_emails_flag_graph_result', return_value=remote_result) as mark_mock:
            response = self.client.post(
                '/api/emails/mark-flag',
                json={
                    'email': 'flagged@example.com',
                    'method': 'graph',
                    'folder': 'inbox',
                    'flagged': True,
                    'items': [{
                        'id': 'flag-graph-1',
                        'folder': 'inbox',
                        'id_mode': 'graph',
                    }],
                }
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['updated_ids'], ['flag-graph-1'])
        self.assertTrue(payload['flagged'])
        mark_mock.assert_called_once()

        state = self._retained_flag_state('flag-graph-1')
        self.assertEqual(state['is_flagged'], 1)
        self.assertIsNotNone(state['updated_at'])

    def test_mark_read_as_unread_updates_retained_graph_row(self):
        self._seed_graph_retained_row('unread-graph-1', is_flagged=0)
        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute(
                '''
                UPDATE retained_normal_mail_messages
                SET is_read = 1
                WHERE account_id = ? AND provider_message_id = ?
                ''',
                (self.account['id'], 'unread-graph-1')
            )
            db.commit()

        remote_result = {
            'success': True,
            'success_count': 1,
            'failed_count': 0,
            'updated_ids': ['unread-graph-1'],
            'errors': [],
        }

        with patch.object(web_outlook_app, 'mark_emails_read_graph_result', return_value=remote_result) as mark_mock:
            response = self.client.post(
                '/api/emails/mark-read',
                json={
                    'email': 'flagged@example.com',
                    'method': 'graph',
                    'folder': 'inbox',
                    'is_read': False,
                    'items': [{
                        'id': 'unread-graph-1',
                        'folder': 'inbox',
                        'id_mode': 'graph',
                    }],
                }
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertFalse(payload['is_read'])
        self.assertEqual(payload['updated_ids'], ['unread-graph-1'])
        mark_mock.assert_called_once()
        self.assertEqual(mark_mock.call_args.kwargs.get('is_read'), False)

        with self.app.app_context():
            db = web_outlook_app.get_db()
            row = db.execute(
                '''
                SELECT is_read
                FROM retained_normal_mail_messages
                WHERE account_id = ? AND provider_message_id = ?
                ''',
                (self.account['id'], 'unread-graph-1')
            ).fetchone()
        self.assertEqual(row['is_read'], 0)

    def test_batch_action_bar_includes_unread_and_flag_controls(self):
        layout_path = os.path.join(ROOT_DIR, 'templates', 'partials', 'index', 'layout.html')
        emails_js_path = os.path.join(ROOT_DIR, 'static', 'js', 'index', '05-emails.js')
        with open(layout_path, encoding='utf-8') as handle:
            layout_html = handle.read()
        with open(emails_js_path, encoding='utf-8') as handle:
            emails_js = handle.read()

        self.assertIn('batchMarkUnreadBtn', layout_html)
        self.assertIn('batchMarkFlagBtn', layout_html)
        self.assertIn('batchUnmarkFlagBtn', layout_html)
        self.assertIn('markSelectedEmailsAsUnread', emails_js)
        self.assertIn('markSelectedEmailsFlag', emails_js)
        self.assertIn("is_read: targetIsRead", emails_js)

    def test_mark_flag_unflag_updates_retained_graph_row(self):
        self._seed_graph_retained_row('flag-graph-2', is_flagged=1)
        remote_result = {
            'success': True,
            'success_count': 1,
            'failed_count': 0,
            'updated_ids': ['flag-graph-2'],
            'errors': [],
        }

        with patch.object(web_outlook_app, 'mark_emails_flag_graph_result', return_value=remote_result):
            response = self.client.post(
                '/api/emails/mark-flag',
                json={
                    'email': 'flagged@example.com',
                    'method': 'graph',
                    'folder': 'inbox',
                    'flagged': False,
                    'items': [{
                        'id': 'flag-graph-2',
                        'folder': 'inbox',
                        'id_mode': 'graph',
                    }],
                }
            )

        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertFalse(payload['flagged'])
        state = self._retained_flag_state('flag-graph-2')
        self.assertEqual(state['is_flagged'], 0)

    def test_mark_flag_remote_failure_does_not_update_retained_graph_row(self):
        self._seed_graph_retained_row('flag-graph-fail', is_flagged=0)
        remote_result = {
            'success': False,
            'success_count': 0,
            'failed_count': 1,
            'updated_ids': [],
            'errors': ['remote failed'],
        }

        with patch.object(web_outlook_app, 'mark_emails_flag_graph_result', return_value=remote_result):
            response = self.client.post(
                '/api/emails/mark-flag',
                json={
                    'email': 'flagged@example.com',
                    'method': 'graph',
                    'folder': 'inbox',
                    'flagged': True,
                    'items': [{
                        'id': 'flag-graph-fail',
                        'folder': 'inbox',
                        'id_mode': 'graph',
                    }],
                }
            )

        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertEqual(payload['error'], 'remote failed')
        state = self._retained_flag_state('flag-graph-fail')
        self.assertEqual(state['is_flagged'], 0)

    def test_list_emails_preserve_is_flagged_from_remote(self):
        remote_result = {
            'success': True,
            'emails': [{
                'id': 'graph-flagged-1',
                'id_mode': 'graph',
                'subject': 'Flagged mail',
                'from': 'sender@example.com',
                'to': 'flagged@example.com',
                'date': '2026-08-11T12:00:00Z',
                'is_read': False,
                'is_flagged': True,
                'has_attachments': False,
                'body_preview': 'preview',
                'folder': 'inbox',
            }],
            'method': 'Graph API',
            'has_more': False,
        }

        with self.app.app_context():
            self.assertTrue(web_outlook_app.set_setting(
                'normal_mail_local_retention_enabled',
                'false',
            ))

        with patch.object(web_outlook_app, 'fetch_account_emails', return_value=remote_result):
            response = self.client.get('/api/emails/flagged@example.com?folder=inbox')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(len(payload['emails']), 1)
        self.assertTrue(payload['emails'][0]['is_flagged'])


if __name__ == '__main__':
    unittest.main()
