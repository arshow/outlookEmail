import importlib
import os
import secrets
import sys
import tempfile
import time
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

    def _trust_sender(self, email, sender):
        with self.app.app_context():
            web_outlook_app.upsert_account_trusted_senders(email['id'], [sender])

    def _email(self, message_id, sender, folder):
        return {
            'id': message_id,
            'id_mode': 'graph',
            'subject': f'Mail {message_id}',
            'from': sender,
            'to': self.account['email'],
            'date': '2026-11-19T10:00:00Z',
            'folder': folder,
        }

    def _fetch_single_folder(self, responses):
        def fetch(account, folder, skip, top, *args, **kwargs):
            return responses[folder]
        return fetch

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

    def test_manual_all_fetch_moves_only_trusted_junk_and_uses_graph_new_id(self):
        self._trust_sender(self.account, 'safe@example.com')
        self._seed_junk_retained_row('trusted-junk', 'Safe <safe@example.com>')
        self._seed_junk_retained_row('other-junk', 'other@example.com')
        responses = {
            'inbox': {'success': True, 'emails': [self._email('existing-inbox', 'inbox@example.com', 'inbox')],
                      'has_more': False, 'request_method': 'graph', 'method': 'Graph API'},
            'junkemail': {'success': True, 'emails': [
                self._email('trusted-junk', 'Safe <safe@example.com>', 'junkemail'),
                self._email('other-junk', 'other@example.com', 'junkemail'),
            ], 'has_more': False, 'request_method': 'graph', 'method': 'Graph API'},
        }

        def move(_client_id, _refresh_token, message_ids, **_kwargs):
            self.assertEqual(message_ids, ['trusted-junk'])
            return {'success': True, 'success_count': 1, 'failed_count': 0,
                    'moved_ids': message_ids, 'moved_id_map': {'trusted-junk': 'trusted-inbox-new'}}

        with patch.object(web_outlook_app, 'fetch_account_folder_emails', side_effect=self._fetch_single_folder(responses)), \
             patch.object(web_outlook_app, 'move_emails_graph', side_effect=move), \
             patch.object(web_outlook_app, 'is_normal_mail_local_retention_enabled', return_value=False):
            request_id = secrets.token_hex(16)
            response = self.client.post(
                f'/api/emails/trust-move@example.com?folder=all&top=20&async=1&progress_id={request_id}'
            )
            self.assertEqual(response.status_code, 202)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                response = self.client.get(f'/api/emails/fetch-progress/{request_id}?result=1')
                job = response.get_json()
                if not job['pending']:
                    break
                time.sleep(0.01)
            else:
                self.fail('Automatic move job did not finish')

        payload = job['result']
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload['success'])
        by_id = {item['id']: item for item in payload['emails']}
        self.assertEqual(by_id['trusted-inbox-new']['folder'], 'inbox')
        self.assertEqual(by_id['other-junk']['folder'], 'junkemail')
        self.assertNotIn('trusted-junk', by_id)
        self.assertEqual(payload['trusted_junk_moved'], 1)
        self.assertFalse(self._retained_exists('trusted-junk', 'junkemail'))
        self.assertTrue(self._retained_exists('other-junk', 'junkemail'))

    def test_read_only_get_does_not_move_trusted_junk(self):
        self._trust_sender(self.account, 'safe@example.com')
        responses = {
            'inbox': {'success': True, 'emails': [], 'has_more': False},
            'junkemail': {'success': True, 'emails': [self._email('still-junk', 'safe@example.com', 'junkemail')],
                          'has_more': False, 'request_method': 'graph'},
        }
        with patch.object(web_outlook_app, 'fetch_account_folder_emails', side_effect=self._fetch_single_folder(responses)), \
             patch.object(web_outlook_app, 'move_emails_graph') as move_mock, \
             patch.object(web_outlook_app, 'is_normal_mail_local_retention_enabled', return_value=False):
            response = self.client.get('/api/emails/trust-move@example.com?folder=all&top=20')

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['id'] for item in payload['emails']], ['still-junk'])
        move_mock.assert_not_called()

    def test_failed_automatic_move_keeps_trusted_mail_in_junk(self):
        self._trust_sender(self.account, 'safe@example.com')
        self._seed_junk_retained_row('failed-move', 'safe@example.com')
        responses = {
            'inbox': {'success': True, 'emails': [], 'has_more': False},
            'junkemail': {'success': True, 'emails': [self._email('failed-move', 'safe@example.com', 'junkemail')],
                          'has_more': False, 'request_method': 'graph'},
        }
        failed = {'success': False, 'success_count': 0, 'failed_count': 1,
                  'moved_ids': [], 'errors': [{'id': 'failed-move', 'error': 'move failed'}]}
        with patch.object(web_outlook_app, 'fetch_account_folder_emails', side_effect=self._fetch_single_folder(responses)), \
             patch.object(web_outlook_app, 'move_emails_graph', return_value=failed), \
             patch.object(web_outlook_app, 'is_normal_mail_local_retention_enabled', return_value=False):
            response = self.client.post('/api/emails/trust-move@example.com?folder=all&top=20')

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['id'] for item in payload['emails']], ['failed-move'])
        self.assertTrue(self._retained_exists('failed-move', 'junkemail'))

    def test_inbox_refresh_moves_trusted_junk_before_fetching_inbox(self):
        self._trust_sender(self.account, 'safe@example.com')
        sequence = []
        state = {'moved': False}

        def fetch(_account, folder, _skip, _top, *_args, **_kwargs):
            sequence.append(('fetch', folder))
            if folder == 'junkemail':
                return {'success': True, 'emails': [self._email('inbox-rescue', 'safe@example.com', 'junkemail')],
                        'has_more': False, 'request_method': 'graph'}
            email = self._email('new-inbox-id', 'safe@example.com', 'inbox')
            return {'success': True, 'emails': [email] if state['moved'] else [], 'has_more': False}

        def move(_client_id, _refresh_token, message_ids, **_kwargs):
            sequence.append(('move', message_ids))
            state['moved'] = True
            return {'success': True, 'success_count': 1, 'failed_count': 0,
                    'moved_ids': message_ids, 'moved_id_map': {'inbox-rescue': 'new-inbox-id'}}

        with patch.object(web_outlook_app, 'fetch_account_folder_emails', side_effect=fetch), \
             patch.object(web_outlook_app, 'move_emails_graph', side_effect=move), \
             patch.object(web_outlook_app, 'is_normal_mail_local_retention_enabled', return_value=False):
            response = self.client.post('/api/emails/trust-move@example.com?folder=inbox&top=20')

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload['success'])
        self.assertEqual(sequence, [('fetch', 'junkemail'), ('move', ['inbox-rescue']), ('fetch', 'inbox')])
        self.assertEqual([item['id'] for item in payload['emails']], ['new-inbox-id'])

    def test_aggregated_fetch_uses_each_mailboxes_own_trusted_senders(self):
        self._trust_sender(self.account, 'safe@example.com')
        with self.app.app_context():
            self.assertTrue(web_outlook_app.add_account(
                'second-trust@example.com', 'password', 'client-id', 'second-refresh-token',
                group_id=1, account_type='outlook', provider='outlook',
            ))
            second = web_outlook_app.get_account_by_email('second-trust@example.com')
            db = web_outlook_app.get_db()
            db.execute('UPDATE accounts SET aggregated_inbox_enabled = 1 WHERE id = ?', (self.account['id'],))
            db.execute('UPDATE accounts SET aggregated_inbox_enabled = 1 WHERE id = ?', (second['id'],))
            db.commit()

        def fetch(account, folder, _skip, _top, *_args, **_kwargs):
            if folder == 'inbox':
                return {'success': True, 'emails': [], 'has_more': False,
                        'request_method': 'graph', 'method': 'Graph API'}
            message_id = 'first-junk' if account['email'] == self.account['email'] else 'second-junk'
            return {'success': True, 'emails': [
                {**self._email(message_id, 'safe@example.com', 'junkemail'),
                 'to': account['email']}
            ], 'has_more': False, 'request_method': 'graph', 'method': 'Graph API'}

        def move(_client_id, _refresh_token, message_ids, **_kwargs):
            self.assertEqual(message_ids, ['first-junk'])
            return {'success': True, 'success_count': 1, 'failed_count': 0,
                    'moved_ids': message_ids, 'moved_id_map': {'first-junk': 'first-inbox-new'}}

        with patch.object(web_outlook_app, 'fetch_account_folder_emails', side_effect=fetch), \
             patch.object(web_outlook_app, 'move_emails_graph', side_effect=move) as move_mock, \
             patch.object(web_outlook_app, 'is_normal_mail_local_retention_enabled', return_value=False):
            response = self.client.post('/api/emails/aggregated?group_id=1&folder=all&top=20')

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload['success'])
        by_id = {item['id']: item for item in payload['emails']}
        self.assertEqual(by_id['first-inbox-new']['account_email'], self.account['email'])
        self.assertEqual(by_id['first-inbox-new']['folder'], 'inbox')
        self.assertEqual(by_id['second-junk']['account_email'], 'second-trust@example.com')
        self.assertEqual(by_id['second-junk']['folder'], 'junkemail')
        move_mock.assert_called_once()


if __name__ == '__main__':
    unittest.main()
