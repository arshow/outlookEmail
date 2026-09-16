import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-email-notes-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')
ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

web_outlook_app = importlib.import_module('web_outlook_app')


class EmailNotesApiTests(unittest.TestCase):
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
            db.execute("DELETE FROM email_notes")
            db.execute("DELETE FROM contact_notes")
            db.execute("DELETE FROM retained_normal_mail_messages")
            db.execute("DELETE FROM accounts WHERE email = ?", ('notes@example.com',))
            db.execute(
                '''
                INSERT INTO accounts (email, client_id, refresh_token, account_type, status)
                VALUES (?, ?, ?, 'outlook', 'active')
                ''',
                ('notes@example.com', 'cid', 'token'),
            )
            account_id = db.execute(
                "SELECT id FROM accounts WHERE email = ?",
                ('notes@example.com',),
            ).fetchone()['id']
            db.execute(
                '''
                INSERT INTO retained_normal_mail_messages (
                    account_id, folder, provider_message_id, id_mode, subject, sender,
                    recipients, received_at, received_at_sort, body, body_type, body_preview,
                    body_cached, list_cached
                ) VALUES (?, 'inbox', ?, 'graph', ?, ?, ?, ?, 3, ?, 'text', ?, 1, 1)
                ''',
                (
                    account_id,
                    'note-msg-1',
                    'Need a follow-up',
                    'buyer@example.com',
                    'notes@example.com',
                    '2026-09-15 10:45:00',
                    'Please ship again',
                    'Please ship again',
                ),
            )
            db.commit()
            self.account_id = account_id

    def _insert_retained(self, message_id, sender, body='Please ship again'):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute(
                '''
                INSERT INTO retained_normal_mail_messages (
                    account_id, folder, provider_message_id, id_mode, subject, sender,
                    recipients, received_at, received_at_sort, body, body_type, body_preview,
                    body_cached, list_cached
                ) VALUES (?, 'inbox', ?, 'graph', ?, ?, ?, ?, 3, ?, 'text', ?, 1, 1)
                ''',
                (
                    self.account_id,
                    message_id,
                    'Need a follow-up',
                    sender,
                    'notes@example.com',
                    '2026-09-15 10:45:00',
                    body,
                    body,
                ),
            )
            db.commit()

    def test_put_note_and_list_attaches_it(self):
        response = self.client.put('/api/emails/note', json={
            'email': 'notes@example.com',
            'message_id': 'note-msg-1',
            'folder': 'inbox',
            'id_mode': 'graph',
            'note': '  已答应补发  ',
        })
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['note'], '已答应补发')
        self.assertEqual(payload['contact_email'], 'buyer@example.com')
        self.assertEqual(payload['contact_note'], '已答应补发')

        listed = self.client.get(
            '/api/emails/notes@example.com',
            query_string={'source': 'local', 'folder': 'inbox', 'skip': 0, 'top': 20},
        )
        listed_payload = listed.get_json()
        self.assertTrue(listed_payload['success'], listed_payload)
        self.assertEqual(listed_payload['emails'][0]['id'], 'note-msg-1')
        self.assertEqual(listed_payload['emails'][0]['note'], '已答应补发')
        self.assertEqual(listed_payload['emails'][0]['contact_email'], 'buyer@example.com')
        self.assertEqual(listed_payload['emails'][0]['contact_note'], '已答应补发')

    def test_note_syncs_to_same_contact_other_emails(self):
        self._insert_retained('note-msg-2', 'buyer@example.com', 'Second order')
        self.client.put('/api/emails/note', json={
            'email': 'notes@example.com',
            'message_id': 'note-msg-1',
            'folder': 'inbox',
            'id_mode': 'graph',
            'contact': 'buyer@example.com',
            'note': '同一客户',
        })
        listed = self.client.get(
            '/api/emails/notes@example.com',
            query_string={'source': 'local', 'folder': 'inbox', 'skip': 0, 'top': 20},
        )
        payload = listed.get_json()
        notes = {item['id']: item['note'] for item in payload['emails']}
        self.assertEqual(notes['note-msg-1'], '同一客户')
        self.assertEqual(notes['note-msg-2'], '同一客户')

    def test_empty_note_clears_row(self):
        self.client.put('/api/emails/note', json={
            'email': 'notes@example.com',
            'message_id': 'note-msg-1',
            'folder': 'inbox',
            'id_mode': 'graph',
            'note': 'temporary',
        })
        response = self.client.put('/api/emails/note', json={
            'email': 'notes@example.com',
            'message_id': 'note-msg-1',
            'folder': 'inbox',
            'id_mode': 'graph',
            'note': '   ',
        })
        payload = response.get_json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['note'], '')
        with self.app.app_context():
            count = web_outlook_app.get_db().execute(
                'SELECT COUNT(*) AS c FROM email_notes WHERE account_id = ?',
                (self.account_id,),
            ).fetchone()['c']
            contact_count = web_outlook_app.get_db().execute(
                'SELECT COUNT(*) AS c FROM contact_notes WHERE account_id = ?',
                (self.account_id,),
            ).fetchone()['c']
        self.assertEqual(count, 0)
        self.assertEqual(contact_count, 0)

    def test_missing_message_id_is_rejected(self):
        response = self.client.put('/api/emails/note', json={
            'email': 'notes@example.com',
            'folder': 'inbox',
            'note': 'oops',
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()['success'])

    def test_unknown_account_is_not_found(self):
        response = self.client.put('/api/emails/note', json={
            'email': 'missing@example.com',
            'message_id': 'note-msg-1',
            'note': 'oops',
        })
        self.assertEqual(response.status_code, 404)


class EmailNotesFrontendTests(unittest.TestCase):
    def test_list_right_click_and_compose_note_field(self):
        root = Path(ROOT_DIR)
        emails_js = (root / 'static' / 'js' / 'index' / '05-emails.js').read_text(encoding='utf-8')
        compose_js = (root / 'static' / 'js' / 'index' / '13-compose.js').read_text(encoding='utf-8')
        html = (root / 'templates' / 'partials' / 'index' / 'dialogs-primary.html').read_text(encoding='utf-8')

        self.assertIn("container.addEventListener('contextmenu', handleEmailListContextMenu);", emails_js)
        self.assertIn('function showEditEmailNoteModal(email)', emails_js)
        self.assertIn('function editCurrentEmailNote()', emails_js)
        self.assertIn("fetch('/api/emails/note'", emails_js)
        self.assertIn('email-note-snippet', emails_js)
        self.assertIn('function resolveEmailNoteContact(email)', emails_js)
        self.assertIn('function isEmailNoteShopifyPlatformAddress(address)', emails_js)
        self.assertIn('function emailNoteContactMatchesItem(item, contactEmail, accountEmail)', emails_js)
        self.assertIn('contact: String(contact || \'\').trim()', emails_js)
        self.assertIn('id="editEmailNoteContact"', html)
        self.assertIn('id="composeEmailNote"', html)
        self.assertIn('function syncComposeEmailNoteField', compose_js)
        self.assertIn('function saveComposeEmailNote', compose_js)
        self.assertIn('syncComposeEmailNoteField(mode, detail);', compose_js)


if __name__ == '__main__':
    unittest.main()
