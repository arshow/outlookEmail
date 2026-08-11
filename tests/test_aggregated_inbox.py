import importlib
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-aggregated-tests-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

web_outlook_app = importlib.import_module('web_outlook_app')


class AggregatedInboxTests(unittest.TestCase):
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
            db.execute('DELETE FROM account_aliases')
            db.execute('DELETE FROM account_tags')
            db.execute('DELETE FROM accounts')
            db.execute("DELETE FROM groups WHERE name NOT IN ('默认分组', '临时邮箱')")
            db.commit()

            self.assertTrue(web_outlook_app.add_account(
                'alpha@example.com',
                'password',
                'client-id',
                'refresh-token-a',
                group_id=1,
                account_type='outlook',
                provider='outlook',
            ))
            self.assertTrue(web_outlook_app.add_account(
                'beta@example.com',
                'password',
                'client-id',
                'refresh-token-b',
                group_id=1,
                account_type='outlook',
                provider='outlook',
            ))
            self.account_alpha = web_outlook_app.get_account_by_email('alpha@example.com')
            self.account_beta = web_outlook_app.get_account_by_email('beta@example.com')
            db.execute(
                'UPDATE accounts SET remark = ?, aggregated_inbox_enabled = 1 WHERE id = ?',
                ('Alpha 备注', self.account_alpha['id']),
            )
            db.execute(
                'UPDATE accounts SET remark = ?, aggregated_inbox_enabled = 1 WHERE id = ?',
                ('Beta 备注', self.account_beta['id']),
            )
            db.commit()
            self.account_alpha = web_outlook_app.get_account_by_email('alpha@example.com')
            self.account_beta = web_outlook_app.get_account_by_email('beta@example.com')

    def _email_item(self, message_id, date, account_email=None):
        item = {
            'id': message_id,
            'id_mode': 'graph',
            'subject': f'Subject {message_id}',
            'from': 'sender@example.com',
            'to': account_email or 'mailbox@example.com',
            'date': date,
            'is_read': False,
            'has_attachments': False,
            'body_preview': 'preview',
            'folder': 'inbox',
        }
        return item

    def test_aggregated_inbox_merges_and_sorts_by_date(self):
        def fake_fetch(account, folder, skip, top):
            if account['email'] == 'alpha@example.com':
                return {
                    'success': True,
                    'emails': [self._email_item('a-1', '2026-05-27T10:00:00Z', account['email'])],
                    'method': 'Graph API',
                    'has_more': False,
                }
            return {
                'success': True,
                'emails': [self._email_item('b-1', '2026-05-28T10:00:00Z', account['email'])],
                'method': 'Graph API',
                'has_more': True,
            }

        with patch.object(web_outlook_app, 'fetch_account_emails', side_effect=fake_fetch):
            response = self.client.get('/api/emails/aggregated?group_id=1&folder=inbox&skip=0&top=20')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertTrue(payload['has_more'])
        self.assertEqual(payload['accounts_used'], 2)
        self.assertEqual(len(payload['emails']), 2)
        self.assertEqual(payload['emails'][0]['id'], 'b-1')
        self.assertEqual(payload['emails'][0]['account_email'], 'beta@example.com')
        self.assertEqual(payload['emails'][1]['id'], 'a-1')
        self.assertEqual(payload['emails'][1]['account_email'], 'alpha@example.com')
        self.assertEqual(payload['emails'][0]['account_id'], self.account_beta['id'])
        self.assertEqual(payload['emails'][1]['account_id'], self.account_alpha['id'])
        self.assertEqual(payload['emails'][0]['account_remark'], 'Beta 备注')
        self.assertEqual(payload['emails'][1]['account_remark'], 'Alpha 备注')

    def test_aggregated_inbox_partial_failure(self):
        def fake_fetch(account, folder, skip, top):
            if account['email'] == 'alpha@example.com':
                return {
                    'success': True,
                    'emails': [self._email_item('a-ok', '2026-05-27T10:00:00Z', account['email'])],
                    'method': 'Graph API',
                    'has_more': False,
                }
            return {
                'success': False,
                'error': 'token expired',
            }

        with patch.object(web_outlook_app, 'fetch_account_emails', side_effect=fake_fetch):
            response = self.client.get('/api/emails/aggregated?group_id=1&folder=all&skip=0&top=20')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertTrue(payload['partial'])
        self.assertEqual(len(payload['emails']), 1)
        self.assertEqual(payload['emails'][0]['account_email'], 'alpha@example.com')
        self.assertEqual(len(payload['account_errors']), 1)
        self.assertEqual(payload['account_errors'][0]['account_email'], 'beta@example.com')

    def test_aggregated_inbox_requires_group_id(self):
        response = self.client.get('/api/emails/aggregated?folder=all')
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload['success'])

    def test_aggregated_inbox_group_not_found(self):
        response = self.client.get('/api/emails/aggregated?group_id=999999&folder=all')
        self.assertEqual(response.status_code, 404)
        payload = response.get_json()
        self.assertFalse(payload['success'])

    def test_aggregated_inbox_empty_group(self):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute('DELETE FROM accounts')
            db.commit()

        response = self.client.get('/api/emails/aggregated?group_id=1&folder=inbox')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['emails'], [])
        self.assertFalse(payload['has_more'])
        self.assertEqual(payload['accounts_used'], 0)

    def test_aggregated_inbox_skips_inactive_accounts(self):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute(
                "UPDATE accounts SET status = 'inactive' WHERE email = ?",
                ('beta@example.com',),
            )
            db.commit()

        def fake_fetch(account, folder, skip, top):
            self.assertEqual(account['email'], 'alpha@example.com')
            return {
                'success': True,
                'emails': [self._email_item('a-only', '2026-05-27T10:00:00Z', account['email'])],
                'method': 'Graph API',
                'has_more': False,
            }

        with patch.object(web_outlook_app, 'fetch_account_emails', side_effect=fake_fetch) as mocked:
            response = self.client.get('/api/emails/aggregated?group_id=1&folder=inbox')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['accounts_used'], 1)
        self.assertEqual(len(payload['emails']), 1)
        self.assertEqual(mocked.call_count, 1)

    def test_aggregated_inbox_requires_opt_in(self):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute(
                'UPDATE accounts SET aggregated_inbox_enabled = 0 WHERE email = ?',
                ('beta@example.com',),
            )
            db.commit()

        def fake_fetch(account, folder, skip, top):
            self.assertEqual(account['email'], 'alpha@example.com')
            return {
                'success': True,
                'emails': [self._email_item('a-only', '2026-05-27T10:00:00Z', account['email'])],
                'method': 'Graph API',
                'has_more': False,
            }

        with patch.object(web_outlook_app, 'fetch_account_emails', side_effect=fake_fetch) as mocked:
            response = self.client.get('/api/emails/aggregated?group_id=1&folder=inbox')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['accounts_used'], 1)
        self.assertEqual(payload['accounts_total'], 1)
        self.assertEqual(mocked.call_count, 1)

    def test_batch_update_aggregated_inbox(self):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute('UPDATE accounts SET aggregated_inbox_enabled = 0')
            db.commit()

        response = self.client.post('/api/accounts/batch-update-aggregated-inbox', json={
            'account_ids': [self.account_alpha['id'], self.account_beta['id']],
            'aggregated_inbox_enabled': True,
        })
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['updated_count'], 2)

        with self.app.app_context():
            alpha = web_outlook_app.get_account_by_email('alpha@example.com')
            beta = web_outlook_app.get_account_by_email('beta@example.com')
            self.assertTrue(alpha.get('aggregated_inbox_enabled'))
            self.assertTrue(beta.get('aggregated_inbox_enabled'))

    def test_aggregated_inbox_local_source_reads_retention_only(self):
        with self.app.app_context():
            web_outlook_app.set_setting('normal_mail_local_retention_enabled', 'true')
            if hasattr(web_outlook_app, 'clear_normal_mail_local_retention_enabled_cache'):
                web_outlook_app.clear_normal_mail_local_retention_enabled_cache()
            web_outlook_app.upsert_retained_normal_mail_list_items(
                self.account_alpha,
                'inbox',
                [self._email_item('local-a', '2026-05-27T10:00:00Z', 'alpha@example.com')],
            )
            web_outlook_app.upsert_retained_normal_mail_list_items(
                self.account_beta,
                'inbox',
                [self._email_item('local-b', '2026-05-28T10:00:00Z', 'beta@example.com')],
            )

        with patch.object(web_outlook_app, 'fetch_account_emails') as remote_mock:
            response = self.client.get(
                '/api/emails/aggregated?group_id=1&folder=inbox&source=local'
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload.get('source'), 'local')
        self.assertEqual(len(payload['emails']), 2)
        self.assertEqual(payload['emails'][0]['id'], 'local-b')
        self.assertEqual(payload['emails'][1]['id'], 'local-a')
        remote_mock.assert_not_called()

    def test_aggregated_inbox_local_source_requires_retention(self):
        with self.app.app_context():
            web_outlook_app.set_setting('normal_mail_local_retention_enabled', 'false')
            if hasattr(web_outlook_app, 'clear_normal_mail_local_retention_enabled_cache'):
                web_outlook_app.clear_normal_mail_local_retention_enabled_cache()

        response = self.client.get(
            '/api/emails/aggregated?group_id=1&folder=inbox&source=local'
        )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload['success'])

    def test_aggregated_remote_refresh_upserts_retention_and_returns_unread(self):
        with self.app.app_context():
            web_outlook_app.set_setting('normal_mail_local_retention_enabled', 'true')
            if hasattr(web_outlook_app, 'clear_normal_mail_local_retention_enabled_cache'):
                web_outlook_app.clear_normal_mail_local_retention_enabled_cache()

        def fake_fetch(account, folder, skip, top):
            if account['email'] == 'alpha@example.com':
                return {
                    'success': True,
                    'emails': [
                        self._email_item('a-unread', '2026-05-27T10:00:00Z', account['email']),
                        {
                            **self._email_item('a-read', '2026-05-27T09:00:00Z', account['email']),
                            'is_read': True,
                        },
                    ],
                    'method': 'Graph API',
                    'has_more': False,
                }
            return {
                'success': True,
                'emails': [
                    {
                        **self._email_item('b-junk', '2026-05-28T10:00:00Z', account['email']),
                        'folder': 'junkemail',
                        'is_read': False,
                    }
                ],
                'method': 'Graph API',
                'has_more': False,
            }

        with patch.object(web_outlook_app, 'fetch_account_emails', side_effect=fake_fetch):
            response = self.client.get('/api/emails/aggregated?group_id=1&folder=all&skip=0&top=20')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload.get('source'), 'remote')

        unread_by_account = payload.get('unread_by_account') or {}
        self.assertEqual(int(unread_by_account.get(str(self.account_alpha['id']), 0)), 1)
        self.assertEqual(int(unread_by_account.get(str(self.account_beta['id']), 0)), 1)
        self.assertEqual(int(payload.get('unread_total') or 0), 2)

        with self.app.app_context():
            db = web_outlook_app.get_db()
            alpha_rows = db.execute(
                '''
                SELECT provider_message_id, folder, is_read
                FROM retained_normal_mail_messages
                WHERE account_id = ?
                ORDER BY provider_message_id
                ''',
                (self.account_alpha['id'],),
            ).fetchall()
            beta_rows = db.execute(
                '''
                SELECT provider_message_id, folder, is_read
                FROM retained_normal_mail_messages
                WHERE account_id = ?
                ''',
                (self.account_beta['id'],),
            ).fetchall()

        self.assertEqual(len(alpha_rows), 2)
        self.assertEqual({row['provider_message_id'] for row in alpha_rows}, {'a-unread', 'a-read'})
        self.assertEqual(len(beta_rows), 1)
        self.assertEqual(beta_rows[0]['provider_message_id'], 'b-junk')
        self.assertEqual(str(beta_rows[0]['folder']).lower(), 'junkemail')
        self.assertEqual(int(beta_rows[0]['is_read'] or 0), 0)


if __name__ == '__main__':
    unittest.main()
