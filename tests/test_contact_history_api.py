import importlib
import os
import sys
import tempfile
import unittest


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-contact-history-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')
ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

web_outlook_app = importlib.import_module('web_outlook_app')

SHOPIFY_BODY = '''
<div class="primary-message">You received a new message from your online store's contact form.</div>
<div class="form-section"><b>Email:</b> <pre>luca.giust@libero.it</pre></div>
'''


class ContactHistoryApiTests(unittest.TestCase):
    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess['logged_in'] = True
        with self.app.app_context():
            web_outlook_app.init_db()
            web_outlook_app.set_setting('normal_mail_local_retention_enabled', 'true')
            web_outlook_app.clear_normal_mail_local_retention_enabled_cache()
            db = web_outlook_app.get_db()
            db.execute("DELETE FROM retained_normal_mail_messages")
            db.execute("DELETE FROM accounts WHERE email = ?", ('store@example.com',))
            db.execute(
                '''
                INSERT INTO accounts (email, client_id, refresh_token, account_type, status)
                VALUES (?, ?, ?, 'outlook', 'active')
                ''',
                ('store@example.com', 'cid', 'token'),
            )
            account_id = db.execute(
                "SELECT id FROM accounts WHERE email = ?",
                ('store@example.com',),
            ).fetchone()['id']
            db.execute(
                '''
                INSERT INTO retained_normal_mail_messages (
                    account_id, folder, provider_message_id, id_mode, subject, sender,
                    recipients, received_at, received_at_sort, body, body_type, body_preview,
                    body_cached, list_cached
                ) VALUES (?, 'inbox', ?, '', ?, ?, ?, ?, 3, ?, 'html', ?, 1, 1)
                ''',
                (
                    account_id,
                    'shopify-1',
                    'New customer message on September 15, 2026 at 10:45 am',
                    'mailer@shopify.com',
                    'store@example.com',
                    '2026-09-15 10:45:00',
                    SHOPIFY_BODY,
                    'You received a new message',
                ),
            )
            db.commit()
            self.account_id = account_id

    def test_contact_history_matches_shopify_body_email(self):
        response = self.client.get(
            '/api/emails/contact-history',
            query_string={
                'email': 'store@example.com',
                'contact': 'luca.giust@libero.it',
                'message_id': 'shopify-1',
            },
        )
        payload = response.get_json()
        self.assertTrue(payload['success'], payload)
        self.assertTrue(payload['retention_enabled'])
        self.assertEqual(payload['contact_email'], 'luca.giust@libero.it')
        self.assertEqual(payload['thread_strategy'], 'contact_email')
        self.assertEqual(len(payload['emails']), 1)
        self.assertEqual(payload['emails'][0]['id'], 'shopify-1')
        self.assertTrue(payload['emails'][0]['is_current'])
        self.assertEqual(payload['emails'][0]['direction'], 'inbound')

    def test_contact_history_disabled_retention_returns_reason(self):
        with self.app.app_context():
            web_outlook_app.set_setting('normal_mail_local_retention_enabled', 'false')
            web_outlook_app.clear_normal_mail_local_retention_enabled_cache()
        response = self.client.get(
            '/api/emails/contact-history',
            query_string={
                'email': 'store@example.com',
                'contact': 'luca.giust@libero.it',
            },
        )
        payload = response.get_json()
        self.assertTrue(payload['success'], payload)
        self.assertFalse(payload['retention_enabled'])
        self.assertEqual(payload['reason'], 'local_retention_disabled')
        self.assertEqual(payload['emails'], [])


if __name__ == '__main__':
    unittest.main()
