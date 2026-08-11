import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-mail-folders-tests-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

web_outlook_app = importlib.import_module('web_outlook_app')


class MailFolderTreeTests(unittest.TestCase):
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
                'folder@example.com',
                'password',
                'client-id',
                'refresh-token',
                group_id=1,
                account_type='outlook',
                provider='outlook',
            ))
            self.account = web_outlook_app.get_account_by_email('folder@example.com')
            web_outlook_app.clear_mail_folder_cache()

    def test_parse_imap_list_entry_and_tree(self):
        parsed = web_outlook_app.parse_imap_list_entry(
            b'(\\HasNoChildren) "/" "INBOX/Projects"'
        )
        self.assertEqual(parsed['name'], 'INBOX/Projects')
        self.assertEqual(parsed['delimiter'], '/')

        noselect = web_outlook_app.parse_imap_list_entry(
            b'(\\Noselect \\HasChildren) "/" "Archive"'
        )
        nodes = web_outlook_app.build_imap_folder_tree_nodes([
            {'name': 'INBOX', 'delimiter': '/', 'attributes': []},
            {'name': 'INBOX/Projects', 'delimiter': '/', 'attributes': []},
            noselect,
        ])
        by_id = {node['id']: node for node in nodes}
        self.assertTrue(by_id['INBOX']['has_children'])
        self.assertEqual(by_id['INBOX/Projects']['parent_id'], 'INBOX')
        self.assertFalse(by_id['Archive']['selectable'])

    def test_normalize_custom_folder_keys(self):
        self.assertEqual(
            web_outlook_app.normalize_folder_name('graph:AAMk123'),
            'graph:AAMk123',
        )
        self.assertEqual(
            web_outlook_app.normalize_folder_name('imap:INBOX/Work'),
            'imap:INBOX/Work',
        )
        self.assertTrue(web_outlook_app.is_custom_mail_folder_storage_key('graph:x'))

    def test_folders_api_returns_graph_nodes(self):
        fake_nodes = {
            'success': True,
            'folders': [{
                'id': 'fid-1',
                'name': 'Inbox',
                'display_name': 'Inbox',
                'parent_id': None,
                'has_children': False,
                'selectable': True,
                'well_known': 'inbox',
                'provider': 'graph',
                'folder_id': 'fid-1',
            }],
            'provider': 'graph',
            'cached': False,
        }
        with patch.object(web_outlook_app, 'list_account_mail_folders', return_value=fake_nodes):
            response = self.client.get('/api/emails/folder@example.com/folders')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['folders'][0]['folder_id'], 'fid-1')

    def test_email_list_rejects_unknown_folder_id(self):
        with patch.object(
            web_outlook_app,
            'collect_allowed_mail_folder_refs',
            return_value=(set(), set()),
        ):
            response = self.client.get(
                '/api/emails/folder@example.com?folder_id=not-exists'
            )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertIn('folder_id', payload['error'])

    def test_email_list_accepts_validated_folder_id(self):
        with patch.object(
            web_outlook_app,
            'collect_allowed_mail_folder_refs',
            return_value=({'fid-ok'}, set()),
        ), patch.object(
            web_outlook_app,
            'fetch_account_emails',
            return_value={
                'success': True,
                'emails': [{
                    'id': 'm1',
                    'subject': 'Hi',
                    'from': 'a@b.com',
                    'to': 'folder@example.com',
                    'date': '2026-08-11T00:00:00Z',
                    'is_read': False,
                    'is_flagged': False,
                    'has_attachments': False,
                    'body_preview': '',
                    'folder': 'graph:fid-ok',
                    'id_mode': 'graph',
                }],
                'method': 'Graph API',
                'has_more': False,
            },
        ) as fetch_mock:
            response = self.client.get(
                '/api/emails/folder@example.com?folder_id=fid-ok'
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload.get('folder_id'), 'fid-ok')
        fetch_mock.assert_called()
        kwargs = fetch_mock.call_args.kwargs
        self.assertEqual(kwargs.get('folder_id'), 'fid-ok')

    def test_frontend_contract_symbols(self):
        folders_js = Path(
            ROOT_DIR, 'static', 'js', 'index', '14-mail-folders.js'
        ).read_text(encoding='utf-8')
        layout_html = Path(
            ROOT_DIR, 'templates', 'partials', 'index', 'layout.html'
        ).read_text(encoding='utf-8')
        self.assertIn('loadMailFolderTree', folders_js)
        self.assertIn('buildMailFolderListParams', folders_js)
        self.assertIn('mailFolderTreeCacheByAccount', folders_js)
        self.assertIn('mailFolderTree', layout_html)
        self.assertIn('folder_id', folders_js)

        emails_js = Path(
            ROOT_DIR, 'static', 'js', 'index', '05-emails.js'
        ).read_text(encoding='utf-8')
        self.assertIn('isNormalMailLocalRetentionEnabled()', emails_js)
        self.assertIn('!forceRefresh', emails_js)
        self.assertIn('tryRenderLocalRetainedEmails', emails_js)


if __name__ == '__main__':
    unittest.main()
