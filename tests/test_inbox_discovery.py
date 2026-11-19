import importlib
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-inbox-discovery-tests-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

web_outlook_app = importlib.import_module('web_outlook_app')


class InboxDiscoveryTests(unittest.TestCase):
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
            db.execute('DELETE FROM account_trusted_senders')
            db.execute('DELETE FROM account_aliases')
            db.execute('DELETE FROM account_tags')
            db.execute('DELETE FROM accounts')
            db.execute("DELETE FROM groups WHERE name NOT IN ('默认分组', '临时邮箱')")
            db.commit()

            self.assertTrue(web_outlook_app.add_account(
                'poll@example.com',
                'password',
                'client-id',
                'refresh-token',
                group_id=1,
                account_type='outlook',
                provider='outlook',
            ))
            self.account = web_outlook_app.get_account_by_email('poll@example.com')
            web_outlook_app.set_setting('normal_mail_local_retention_enabled', 'true')
            web_outlook_app.set_setting('inbox_poll_scheduler_enabled', 'true')
            if hasattr(web_outlook_app, 'clear_normal_mail_local_retention_enabled_cache'):
                web_outlook_app.clear_normal_mail_local_retention_enabled_cache()

    def test_new_account_defaults_inbox_poll_enabled(self):
        self.assertTrue(self.account.get('inbox_poll_enabled', True))
        with self.app.app_context():
            db = web_outlook_app.get_db()
            row = db.execute(
                'SELECT inbox_poll_enabled FROM accounts WHERE id = ?',
                (self.account['id'],)
            ).fetchone()
        self.assertEqual(int(row['inbox_poll_enabled']), 1)

    def test_batch_update_inbox_poll_api(self):
        response = self.client.post('/api/accounts/batch-update-inbox-poll', json={
            'account_ids': [self.account['id']],
            'inbox_poll_enabled': False,
        })
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['updated_count'], 1)

        with self.app.app_context():
            account = web_outlook_app.get_account_by_email('poll@example.com')
        self.assertFalse(bool(account.get('inbox_poll_enabled')))

    def test_job_skips_when_local_retention_disabled(self):
        with self.app.app_context():
            web_outlook_app.set_setting('normal_mail_local_retention_enabled', 'false')
            if hasattr(web_outlook_app, 'clear_normal_mail_local_retention_enabled_cache'):
                web_outlook_app.clear_normal_mail_local_retention_enabled_cache()

        result = web_outlook_app.process_inbox_discovery_job()
        self.assertTrue(result.get('skipped'))
        self.assertEqual(result.get('reason'), 'local_retention_disabled')

    def test_job_skips_disabled_account(self):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute(
                'UPDATE accounts SET inbox_poll_enabled = 0 WHERE id = ?',
                (self.account['id'],)
            )
            db.commit()

        with patch.object(web_outlook_app, 'fetch_account_folder_emails') as fetch_mock:
            result = web_outlook_app.process_inbox_discovery_job()

        self.assertTrue(result.get('success'))
        self.assertFalse(result.get('skipped'))
        self.assertEqual(result.get('accounts_total'), 0)
        fetch_mock.assert_not_called()

    def _folder_fetch_result(self, emails):
        return {
            'success': True,
            'emails': emails,
            'method': 'Graph API',
            'has_more': False,
        }

    def _trust_sender(self, sender_email):
        with self.app.app_context():
            return web_outlook_app.upsert_account_trusted_senders(
                self.account['id'], [sender_email]
            )

    def _fake_folder_fetch(self, inbox_emails=None, junk_emails=None, junk_error=None):
        inbox_emails = list(inbox_emails or [])
        junk_emails = list(junk_emails or [])

        def fake_fetch(account, folder, skip, top, **kwargs):
            if folder == 'inbox':
                return self._folder_fetch_result(inbox_emails)
            if folder == 'junkemail':
                if junk_error:
                    return {'success': False, 'error': junk_error, 'emails': []}
                return self._folder_fetch_result(junk_emails)
            return {'success': False, 'error': f'unexpected folder {folder}', 'emails': []}

        return fake_fetch

    def test_discover_account_upserts_and_publishes_event(self):
        remote_emails = [{
            'id': 'msg-new-1',
            'id_mode': 'graph',
            'subject': 'Hello',
            'from': 'sender@example.com',
            'to': 'poll@example.com',
            'date': '2026-08-11T12:00:00Z',
            'is_read': False,
            'is_flagged': False,
            'has_attachments': False,
            'body_preview': 'preview',
            'folder': 'inbox',
        }]
        events = []
        fetched_folders = []

        def capture_event(event):
            events.append(event)
            return 1

        def fake_fetch(account, folder, skip, top, **kwargs):
            fetched_folders.append(folder)
            return self._fake_folder_fetch(inbox_emails=remote_emails)(account, folder, skip, top, **kwargs)

        with self.app.app_context():
            with patch.object(
                web_outlook_app,
                'fetch_account_folder_emails',
                side_effect=fake_fetch,
            ), patch.object(web_outlook_app, 'publish_inbox_discovery_event', side_effect=capture_event):
                result = web_outlook_app.discover_account_inbox_mail(self.account, top=20)

        self.assertTrue(result['success'])
        self.assertEqual(result['new_count'], 1)
        self.assertEqual(fetched_folders, ['inbox', 'junkemail'])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['type'], 'new_mail')
        self.assertEqual(events[0]['folder'], 'inbox')
        self.assertEqual(events[0]['account_email'], 'poll@example.com')
        self.assertEqual(events[0]['emails'][0]['id'], 'msg-new-1')

        with self.app.app_context():
            db = web_outlook_app.get_db()
            row = db.execute(
                '''
                SELECT provider_message_id
                FROM retained_normal_mail_messages
                WHERE account_id = ? AND provider_message_id = ?
                ''',
                (self.account['id'], 'msg-new-1')
            ).fetchone()
            account_row = db.execute(
                'SELECT inbox_poll_last_checked_at FROM accounts WHERE id = ?',
                (self.account['id'],)
            ).fetchone()

        self.assertIsNotNone(row)
        self.assertIsNotNone(account_row['inbox_poll_last_checked_at'])

    def test_discover_account_also_fetches_junkemail(self):
        junk_emails = [{
            'id': 'msg-junk-1',
            'id_mode': 'graph',
            'subject': 'Maybe spam',
            'from': 'promo@example.com',
            'to': 'poll@example.com',
            'date': '2026-08-11T12:05:00Z',
            'is_read': False,
            'is_flagged': False,
            'has_attachments': False,
            'body_preview': 'sale',
            'folder': 'junkemail',
        }]
        events = []

        def capture_event(event):
            events.append(event)
            return 1

        with self.app.app_context():
            with patch.object(
                web_outlook_app,
                'fetch_account_folder_emails',
                side_effect=self._fake_folder_fetch(junk_emails=junk_emails),
            ), patch.object(web_outlook_app, 'publish_inbox_discovery_event', side_effect=capture_event):
                result = web_outlook_app.discover_account_inbox_mail(self.account, top=20)

        self.assertTrue(result['success'])
        self.assertEqual(result['new_count'], 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['folder'], 'junkemail')
        self.assertEqual(events[0]['emails'][0]['id'], 'msg-junk-1')

        with self.app.app_context():
            db = web_outlook_app.get_db()
            row = db.execute(
                '''
                SELECT folder, provider_message_id
                FROM retained_normal_mail_messages
                WHERE account_id = ? AND provider_message_id = ?
                ''',
                (self.account['id'], 'msg-junk-1')
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(str(row['folder']).lower(), 'junkemail')

    def test_discover_continues_when_junkemail_fetch_fails(self):
        inbox_emails = [{
            'id': 'msg-inbox-ok',
            'id_mode': 'graph',
            'subject': 'Hello',
            'from': 'sender@example.com',
            'to': 'poll@example.com',
            'date': '2026-08-11T12:00:00Z',
            'is_read': False,
            'is_flagged': False,
            'has_attachments': False,
            'body_preview': 'preview',
            'folder': 'inbox',
        }]

        with self.app.app_context():
            with patch.object(
                web_outlook_app,
                'fetch_account_folder_emails',
                side_effect=self._fake_folder_fetch(
                    inbox_emails=inbox_emails,
                    junk_error='junk mailbox missing',
                ),
            ):
                result = web_outlook_app.discover_account_inbox_mail(self.account, top=20)

        self.assertTrue(result['success'])
        self.assertEqual(result['new_count'], 1)
        self.assertIn('junkemail', result.get('folder_errors') or {})

    def test_discover_moves_trusted_junk_and_caches_graph_move_in_inbox(self):
        self._trust_sender('trusted@example.com')
        junk_email = {
            'id': 'junk-original-id',
            'id_mode': 'graph',
            'internet_message_id': '<trusted-message@example.com>',
            'subject': 'Trusted sender mail',
            'from': 'Trusted sender <trusted@example.com>',
            'to': 'poll@example.com',
            'date': '2026-08-11T12:05:00Z',
            'is_read': False,
            'is_flagged': False,
            'has_attachments': False,
            'body_preview': 'hello',
            'folder': 'junkemail',
        }
        inbox_email = {**junk_email, 'id': 'inbox-new-graph-id', 'folder': 'inbox'}
        calls = []
        events = []

        def fake_fetch(_account, folder, _skip, _top, **_kwargs):
            calls.append(('fetch', folder))
            if folder == 'junkemail':
                return {
                    **self._folder_fetch_result([junk_email]),
                    'request_method': 'graph',
                }
            if folder == 'inbox':
                return {
                    **self._folder_fetch_result([inbox_email]),
                    'request_method': 'graph',
                }
            return {'success': False, 'error': f'unexpected folder {folder}', 'emails': []}

        def fake_graph_move(_client_id, _refresh_token, message_ids, **_kwargs):
            calls.append(('move', list(message_ids)))
            return {
                'success': True,
                'success_count': 1,
                'failed_count': 0,
                'moved_ids': list(message_ids),
                'moved_id_map': {'junk-original-id': 'inbox-new-graph-id'},
                'errors': [],
            }

        with self.app.app_context():
            with patch.object(web_outlook_app, 'fetch_account_folder_emails', side_effect=fake_fetch), \
                    patch.object(web_outlook_app, 'move_emails_graph', side_effect=fake_graph_move), \
                    patch.object(web_outlook_app, 'publish_inbox_discovery_event', side_effect=lambda event: events.append(event) or 1):
                result = web_outlook_app.discover_account_inbox_mail(self.account, top=20)

        self.assertTrue(result['success'])
        self.assertEqual(calls, [
            ('fetch', 'junkemail'),
            ('move', ['junk-original-id']),
            ('fetch', 'inbox'),
        ])
        self.assertEqual(result['new_count'], 1)
        self.assertEqual(
            [(item['id'], item['folder']) for item in result['emails']],
            [('inbox-new-graph-id', 'inbox')],
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['folder'], 'inbox')
        self.assertEqual(events[0]['emails'][0]['id'], 'inbox-new-graph-id')

        with self.app.app_context():
            rows = web_outlook_app.get_db().execute(
                '''
                SELECT folder, provider_message_id
                FROM retained_normal_mail_messages
                WHERE account_id = ? AND provider_message_id IN (?, ?)
                ORDER BY folder, provider_message_id
                ''',
                (self.account['id'], 'junk-original-id', 'inbox-new-graph-id')
            ).fetchall()
        self.assertEqual(
            [(str(row['folder']).lower(), row['provider_message_id']) for row in rows],
            [('inbox', 'inbox-new-graph-id')],
        )

    def test_discover_leaves_untrusted_sender_in_junk(self):
        junk_email = {
            'id': 'untrusted-junk-id',
            'id_mode': 'graph',
            'subject': 'Untrusted sender mail',
            'from': 'other@example.com',
            'to': 'poll@example.com',
            'date': '2026-08-11T12:05:00Z',
            'is_read': False,
            'is_flagged': False,
            'has_attachments': False,
            'body_preview': 'hello',
            'folder': 'junkemail',
        }
        events = []

        with self.app.app_context():
            with patch.object(
                web_outlook_app,
                'fetch_account_folder_emails',
                side_effect=self._fake_folder_fetch(junk_emails=[junk_email]),
            ), patch.object(web_outlook_app, 'move_emails_graph') as move_mock, \
                    patch.object(web_outlook_app, 'publish_inbox_discovery_event', side_effect=lambda event: events.append(event) or 1):
                result = web_outlook_app.discover_account_inbox_mail(self.account, top=20)

        self.assertTrue(result['success'])
        self.assertEqual(result['new_count'], 1)
        self.assertEqual([(item['id'], item['folder']) for item in result['emails']],
                         [('untrusted-junk-id', 'junkemail')])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['folder'], 'junkemail')
        move_mock.assert_not_called()
        with self.app.app_context():
            row = web_outlook_app.get_db().execute(
                '''
                SELECT folder FROM retained_normal_mail_messages
                WHERE account_id = ? AND provider_message_id = ?
                ''', (self.account['id'], 'untrusted-junk-id')
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(str(row['folder']).lower(), 'junkemail')

    def test_discover_leaves_trusted_junk_when_move_fails(self):
        self._trust_sender('trusted@example.com')
        junk_email = {
            'id': 'trusted-junk-move-failed',
            'id_mode': 'graph',
            'subject': 'Trusted sender mail',
            'from': 'trusted@example.com',
            'to': 'poll@example.com',
            'date': '2026-08-11T12:05:00Z',
            'is_read': False,
            'is_flagged': False,
            'has_attachments': False,
            'body_preview': 'hello',
            'folder': 'junkemail',
        }
        calls = []
        events = []

        def fake_fetch(_account, folder, _skip, _top, **_kwargs):
            calls.append(('fetch', folder))
            if folder == 'junkemail':
                return {
                    **self._folder_fetch_result([junk_email]),
                    'request_method': 'graph',
                }
            return {
                **self._folder_fetch_result([]),
                'request_method': 'graph',
            }

        def failed_graph_move(_client_id, _refresh_token, message_ids, **_kwargs):
            calls.append(('move', list(message_ids)))
            return {
                'success': False,
                'success_count': 0,
                'failed_count': len(message_ids),
                'moved_ids': [],
                'moved_id_map': {},
                'errors': [{'id': message_id, 'error': 'move failed'} for message_id in message_ids],
            }

        with self.app.app_context():
            with patch.object(web_outlook_app, 'fetch_account_folder_emails', side_effect=fake_fetch), \
                    patch.object(web_outlook_app, 'move_emails_graph', side_effect=failed_graph_move), \
                    patch.object(web_outlook_app, 'publish_inbox_discovery_event', side_effect=lambda event: events.append(event) or 1):
                result = web_outlook_app.discover_account_inbox_mail(self.account, top=20)

        self.assertTrue(result['success'])
        self.assertEqual(calls, [
            ('fetch', 'junkemail'),
            ('move', ['trusted-junk-move-failed']),
            ('fetch', 'inbox'),
        ])
        self.assertEqual(result['new_count'], 1)
        self.assertEqual([(item['id'], item['folder']) for item in result['emails']],
                         [('trusted-junk-move-failed', 'junkemail')])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['folder'], 'junkemail')
        with self.app.app_context():
            row = web_outlook_app.get_db().execute(
                '''
                SELECT folder FROM retained_normal_mail_messages
                WHERE account_id = ? AND provider_message_id = ?
                ''', (self.account['id'], 'trusted-junk-move-failed')
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(str(row['folder']).lower(), 'junkemail')

    def test_trigger_api_requires_retention(self):
        with self.app.app_context():
            web_outlook_app.set_setting('normal_mail_local_retention_enabled', 'false')
            if hasattr(web_outlook_app, 'clear_normal_mail_local_retention_enabled_cache'):
                web_outlook_app.clear_normal_mail_local_retention_enabled_cache()

        response = self.client.post('/api/accounts/trigger-inbox-discovery', json={})
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertIn('本地保留', payload['error'])

    def test_sse_connected_event(self):
        received = []

        def reader():
            with self.client.get(
                '/api/emails/inbox-discovery/events',
                buffered=False,
            ) as response:
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.mimetype.startswith('text/event-stream'))
                chunk = next(response.response)
                text = chunk.decode('utf-8') if isinstance(chunk, (bytes, bytearray)) else str(chunk)
                received.append(text)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        thread.join(timeout=3)
        self.assertTrue(received)
        self.assertIn('connected', received[0])

    def test_frontend_contract_symbols(self):
        emails_js = Path(ROOT_DIR, 'static', 'js', 'index', '05-emails.js').read_text(encoding='utf-8')
        settings_html = Path(
            ROOT_DIR, 'templates', 'partials', 'index', 'dialogs-management.html'
        ).read_text(encoding='utf-8')
        layout_html = Path(
            ROOT_DIR, 'templates', 'partials', 'index', 'layout.html'
        ).read_text(encoding='utf-8')
        primary_html = Path(
            ROOT_DIR, 'templates', 'partials', 'index', 'dialogs-primary.html'
        ).read_text(encoding='utf-8')

        self.assertIn('startInboxDiscoveryEventSource', emails_js)
        self.assertIn('handleInboxDiscoveryNewMailEvent', emails_js)
        self.assertIn("viewFolder === 'junkemail' && eventFolder !== 'junkemail'", emails_js)
        self.assertIn('queuePendingNewMailSync', emails_js)
        self.assertIn("invalidateEmailListCache(accountEmail, 'all')", emails_js)
        self.assertIn('inboxPollSchedulerEnabled', settings_html)
        self.assertIn('inboxPollConcurrency', settings_html)
        self.assertIn('editInboxPollEnabled', primary_html)
        self.assertIn('batchEnableInboxPollBtn', layout_html)

    def test_job_uses_limited_concurrency(self):
        with self.app.app_context():
            web_outlook_app.set_setting('inbox_poll_concurrency', '3')
            self.assertTrue(web_outlook_app.add_account(
                'poll2@example.com',
                'password',
                'client-id',
                'refresh-token-2',
                group_id=1,
                account_type='outlook',
                provider='outlook',
            ))

        seen = []

        def fake_discover(account, top):
            seen.append(account.get('email'))
            return {'success': True, 'new_count': 0}

        with patch.object(web_outlook_app, 'discover_account_inbox_mail', side_effect=fake_discover), \
                patch.object(web_outlook_app, 'normalize_inbox_poll_account_delay_seconds', return_value=0):
            result = web_outlook_app.process_inbox_discovery_job()

        self.assertTrue(result.get('success'))
        self.assertEqual(result.get('concurrency'), 3)
        self.assertEqual(result.get('accounts_processed'), 2)
        self.assertEqual(sorted(seen), ['poll2@example.com', 'poll@example.com'])


if __name__ == '__main__':
    unittest.main()
