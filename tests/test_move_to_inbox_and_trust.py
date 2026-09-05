import importlib
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-move-trust-tests-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')
ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

web_outlook_app = importlib.import_module('web_outlook_app')


class MoveToInboxAndTrustTests(unittest.TestCase):
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
            db.execute('DELETE FROM account_trusted_senders')
            db.execute('DELETE FROM retained_normal_mail_messages')
            db.execute('DELETE FROM account_aliases')
            db.execute('DELETE FROM account_tags')
            db.execute('DELETE FROM accounts')
            db.execute("DELETE FROM groups WHERE name NOT IN ('默认分组', '临时邮箱')")
            db.commit()
            added = web_outlook_app.add_account(
                'trust-move@example.com',
                'password',
                'client-id',
                'refresh-token',
                group_id=1,
                account_type='outlook',
                provider='outlook',
            )
            self.assertTrue(added)
            self.account = web_outlook_app.get_account_by_email('trust-move@example.com')

    def _seed_junk_retained_row(self, message_id='junk-1', sender='Store <store+1@shopifyemail.com>'):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute(
                '''
                INSERT INTO retained_normal_mail_messages (
                    account_id, folder, provider_message_id, id_mode,
                    subject, sender, recipients, received_at, is_read, is_flagged, list_cached
                )
                VALUES (?, 'junkemail', ?, 'graph',
                        'Order placed', ?,
                        'trust-move@example.com', '2026-08-14T11:20:00Z', 0, 0, 1)
                ''',
                (self.account['id'], message_id, sender)
            )
            db.commit()

    def _trusted_senders(self):
        with self.app.app_context():
            return web_outlook_app.list_account_trusted_senders(self.account['id'])

    def _retained_exists(self, message_id, folder='junkemail'):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            row = db.execute(
                '''
                SELECT id FROM retained_normal_mail_messages
                WHERE account_id = ? AND folder = ? AND provider_message_id = ? AND id_mode = 'graph'
                ''',
                (self.account['id'], folder, message_id)
            ).fetchone()
        return row is not None

    def test_parse_trusted_sender_email(self):
        self.assertEqual(
            web_outlook_app.parse_trusted_sender_email('Store <store+1@shopifyemail.com>'),
            'store+1@shopifyemail.com'
        )
        self.assertEqual(
            web_outlook_app.parse_trusted_sender_email({'emailAddress': {'address': 'A@Example.com'}}),
            'a@example.com'
        )
        self.assertEqual(web_outlook_app.parse_trusted_sender_email('not-an-email'), '')

    def test_upsert_account_trusted_senders_is_per_account(self):
        with self.app.app_context():
            first = web_outlook_app.upsert_account_trusted_senders(
                self.account['id'],
                ['alpha@example.com', 'Alpha@example.com', 'bad']
            )
            again = web_outlook_app.upsert_account_trusted_senders(
                self.account['id'],
                ['alpha@example.com', 'beta@example.com']
            )
        self.assertEqual(first, ['alpha@example.com'])
        self.assertEqual(again, ['alpha@example.com', 'beta@example.com'])
        self.assertEqual(self._trusted_senders(), ['alpha@example.com', 'beta@example.com'])

    def test_move_to_inbox_and_trust_graph_success(self):
        self._seed_junk_retained_row('junk-graph-1', 'Store <notify@example.com>')
        remote_result = {
            'success': True,
            'success_count': 1,
            'failed_count': 0,
            'moved_ids': ['junk-graph-1'],
            'updated_ids': ['junk-graph-1'],
            'deleted_ids': ['junk-graph-1'],
            'moved_id_map': {'junk-graph-1': 'inbox-new-1'},
            'errors': [],
        }
        with patch.object(web_outlook_app, 'move_emails_graph', return_value=remote_result) as move_mock:
            response = self.client.post('/api/emails/move-to-inbox-and-trust', json={
                'email': 'trust-move@example.com',
                'method': 'graph',
                'folder': 'junkemail',
                'items': [{
                    'id': 'junk-graph-1',
                    'folder': 'junkemail',
                    'id_mode': 'graph',
                    'from': 'Store <notify@example.com>',
                }],
            })

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['moved_ids'], ['junk-graph-1'])
        self.assertEqual(payload['trusted_senders'], ['notify@example.com'])
        self.assertEqual(payload['trusted_count'], 1)
        move_mock.assert_called_once()
        self.assertFalse(self._retained_exists('junk-graph-1', 'junkemail'))
        self.assertEqual(self._trusted_senders(), ['notify@example.com'])

        list_response = self.client.get('/api/accounts/trust-move@example.com/trusted-senders')
        self.assertEqual(list_response.status_code, 200)
        list_payload = list_response.get_json()
        self.assertTrue(list_payload['success'])
        self.assertEqual(list_payload['trusted_senders'], ['notify@example.com'])

    def test_move_to_inbox_and_trust_rejects_non_junk(self):
        response = self.client.post('/api/emails/move-to-inbox-and-trust', json={
            'email': 'trust-move@example.com',
            'method': 'graph',
            'items': [{
                'id': 'inbox-1',
                'folder': 'inbox',
                'id_mode': 'graph',
                'from': 'a@example.com',
            }],
        })
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertIn('垃圾邮件', payload.get('error', ''))

    def test_move_to_inbox_and_trust_remote_failure_skips_trust(self):
        self._seed_junk_retained_row('junk-fail-1', 'fail@example.com')
        remote_result = {
            'success': False,
            'success_count': 0,
            'failed_count': 1,
            'moved_ids': [],
            'updated_ids': [],
            'deleted_ids': [],
            'errors': ['boom'],
            'error': 'boom',
        }
        with patch.object(web_outlook_app, 'move_emails_graph', return_value=remote_result):
            response = self.client.post('/api/emails/move-to-inbox-and-trust', json={
                'email': 'trust-move@example.com',
                'method': 'graph',
                'items': [{
                    'id': 'junk-fail-1',
                    'folder': 'junkemail',
                    'id_mode': 'graph',
                    'from': 'fail@example.com',
                }],
            })

        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertEqual(payload.get('trusted_count', 0), 0)
        self.assertEqual(self._trusted_senders(), [])
        self.assertTrue(self._retained_exists('junk-fail-1', 'junkemail'))


if __name__ == '__main__':
    unittest.main()
