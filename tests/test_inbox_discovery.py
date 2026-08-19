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

        def capture_event(event):
            events.append(event)
            return 1

        with self.app.app_context():
            with patch.object(
                web_outlook_app,
                'fetch_account_folder_emails',
                return_value={
                    'success': True,
                    'emails': remote_emails,
                    'method': 'Graph API',
                    'has_more': False,
                },
            ), patch.object(web_outlook_app, 'publish_inbox_discovery_event', side_effect=capture_event):
                result = web_outlook_app.discover_account_inbox_mail(self.account, top=20)

        self.assertTrue(result['success'])
        self.assertEqual(result['new_count'], 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['type'], 'new_mail')
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
