import importlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-mailbox-export-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

web_outlook_app = importlib.import_module('web_outlook_app')


class FakeGraphResponse:
    def __init__(self, status_code, payload=None, content=b''):
        self.status_code = status_code
        self._payload = {} if payload is None else payload
        self.content = content

    def json(self):
        return self._payload


class MailboxExportTests(unittest.TestCase):
    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        web_outlook_app.reset_mailbox_export_state()

        with self.app.app_context():
            web_outlook_app.init_db()
            web_outlook_app.set_setting(
                web_outlook_app.LOGIN_SESSION_VERSION_SETTING_KEY,
                web_outlook_app.DEFAULT_LOGIN_SESSION_VERSION,
            )

        with self.client.session_transaction() as sess:
            sess['logged_in'] = True
            sess['login_session_version'] = web_outlook_app.DEFAULT_LOGIN_SESSION_VERSION

    def tearDown(self):
        web_outlook_app.reset_mailbox_export_state()

    def _insert_account(self, email_addr: str) -> int:
        with self.app.app_context():
            db = web_outlook_app.get_db()
            cursor = db.execute(
                '''
                INSERT INTO accounts (
                    email, password, client_id, refresh_token,
                    group_id, remark, status, account_type, provider,
                    imap_host, imap_port, imap_password, forward_enabled
                )
                VALUES (?, '', 'client-id', 'refresh-token', 1, '', 'active', 'outlook', 'outlook', '', 993, '', 0)
                ''',
                (email_addr,),
            )
            db.commit()
            return int(cursor.lastrowid)

    def test_export_requires_login(self):
        anonymous = self.app.test_client()
        response = anonymous.post('/api/emails/person@example.com/export', json={'scope': 'mailbox'})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.get_json()['success'])

    def test_export_missing_account_returns_404(self):
        response = self.client.post('/api/emails/missing-export@example.com/export', json={'scope': 'mailbox'})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.get_json()['success'])

    def test_export_rejects_unknown_scope(self):
        self._insert_account('scope-export@example.com')
        response = self.client.post('/api/emails/scope-export@example.com/export', json={'scope': 'everything'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('scope', response.get_json()['error'])

    def test_folder_scope_all_uses_inbox_and_junk_without_listing_folders(self):
        account = {'id': 1, 'email': 'folder-export@example.com', 'account_type': 'outlook'}
        with patch.object(web_outlook_app, 'list_account_mail_folders', side_effect=AssertionError('should not list')):
            result = web_outlook_app.build_mailbox_export_targets(account, 'folder', folder='all')
        self.assertTrue(result['success'])
        self.assertEqual([item['folder'] for item in result['targets']], ['inbox', 'junkemail'])

    def test_mailbox_scope_orders_folders_and_falls_back_when_listing_fails(self):
        account = {'id': 2, 'email': 'ordered-export@example.com', 'account_type': 'outlook'}
        nodes = [
            {'id': 'junk-id', 'display_name': '垃圾邮件', 'provider': 'graph', 'folder_id': 'junk-id', 'selectable': True, 'well_known': 'junkemail'},
            {'id': 'custom-id', 'display_name': '项目', 'provider': 'graph', 'folder_id': 'custom-id', 'selectable': True, 'well_known': ''},
            {'id': 'inbox-id', 'display_name': '收件箱', 'provider': 'graph', 'folder_id': 'inbox-id', 'selectable': True, 'well_known': 'inbox'},
            {'id': 'hidden', 'display_name': '不可选', 'provider': 'graph', 'folder_id': 'hidden', 'selectable': False, 'well_known': ''},
        ]
        with patch.object(web_outlook_app, 'list_account_mail_folders', return_value={'success': True, 'folders': nodes}):
            ordered = web_outlook_app.build_mailbox_export_targets(account, 'mailbox')
        self.assertEqual(
            [item['label'] for item in ordered['targets']],
            ['收件箱', '垃圾邮件', '项目'],
        )
        self.assertEqual(ordered['targets'][0]['folder_id'], 'inbox-id')

        with patch.object(web_outlook_app, 'list_account_mail_folders', return_value={'success': False, 'error': 'down'}):
            fallback = web_outlook_app.build_mailbox_export_targets(account, 'mailbox')
        self.assertEqual(
            [item['folder'] for item in fallback['targets']],
            ['inbox', 'junkemail', 'deleteditems'],
        )
        self.assertTrue(fallback['folder_list_fallback'])

    def test_graph_list_follows_next_link_and_keeps_newest_when_capped(self):
        account = {
            'email': 'graph-export@example.com',
            'client_id': 'client-id',
            'refresh_token': 'refresh-token',
            'account_type': 'outlook',
        }
        ctx = web_outlook_app.MailboxExportContext()
        ctx.proxy_url = ''
        ctx.fallback_proxy_urls = []
        target = {'label': '收件箱', 'folder': 'inbox', 'folder_id': '', 'channel': 'graph', 'well_known': 'inbox'}
        pages = [
            FakeGraphResponse(200, {
                'value': [{
                    'id': 'newer',
                    'subject': 'Newer',
                    'receivedDateTime': '2026-11-02T00:00:00Z',
                    'from': {'emailAddress': {'address': 'new@example.com'}},
                }],
                '@odata.nextLink': 'https://graph.example/messages?skip=1',
            }),
            FakeGraphResponse(200, {
                'value': [{
                    'id': 'older',
                    'subject': 'Older',
                    'receivedDateTime': '2026-10-01T00:00:00Z',
                }],
            }),
        ]

        def fake_get(url, **_kwargs):
            return pages.pop(0)

        with patch.object(web_outlook_app, 'get_access_token_graph_result', return_value={'success': True, 'access_token': 'token'}), \
                patch.object(web_outlook_app, 'get_with_proxy_fallback', side_effect=fake_get):
            listed = web_outlook_app.list_graph_export_refs(account, target, ctx, limit=10)
        self.assertTrue(listed['success'])
        self.assertEqual([item['id'] for item in listed['refs']], ['newer', 'older'])
        self.assertFalse(listed['truncated'])
        self.assertEqual(listed['refs'][0]['from'], 'new@example.com')

        pages = [
            FakeGraphResponse(200, {
                'value': [{'id': 'newer', 'subject': 'Newer', 'receivedDateTime': '2026-11-02T00:00:00Z'}],
                '@odata.nextLink': 'https://graph.example/messages?skip=1',
            }),
            FakeGraphResponse(200, {'value': [{'id': 'older', 'subject': 'Older'}]}),
        ]
        with patch.object(web_outlook_app, 'get_access_token_graph_result', return_value={'success': True, 'access_token': 'token'}), \
                patch.object(web_outlook_app, 'get_with_proxy_fallback', side_effect=fake_get):
            capped = web_outlook_app.list_graph_export_refs(account, target, ctx, limit=1)
        self.assertEqual([item['id'] for item in capped['refs']], ['newer'])
        self.assertTrue(capped['truncated'])
        self.assertEqual(len(pages), 1)

    def test_graph_list_retries_without_orderby_after_bad_request(self):
        account = {
            'email': 'graph-order-export@example.com',
            'client_id': 'client-id',
            'refresh_token': 'refresh-token',
            'account_type': 'outlook',
        }
        ctx = web_outlook_app.MailboxExportContext()
        ctx.proxy_url = ''
        ctx.fallback_proxy_urls = []
        calls = []

        def fake_get(url, **kwargs):
            calls.append(kwargs.get('params'))
            if len(calls) == 1:
                return FakeGraphResponse(400, {'error': {'message': 'orderby failed'}})
            return FakeGraphResponse(200, {'value': [{'id': 'm1', 'subject': 'Hello'}]})

        with patch.object(web_outlook_app, 'get_access_token_graph_result', return_value={'success': True, 'access_token': 'token'}), \
                patch.object(web_outlook_app, 'get_with_proxy_fallback', side_effect=fake_get):
            listed = web_outlook_app.list_graph_export_refs(
                account,
                {'label': '收件箱', 'folder': 'inbox', 'folder_id': ''},
                ctx,
                limit=5,
            )
        self.assertTrue(listed['success'])
        self.assertEqual(listed['refs'][0]['id'], 'm1')
        self.assertIn('$orderby', calls[0])
        self.assertNotIn('$orderby', calls[1])

    def test_zip_contains_eml_manifest_and_failed_message_note(self):
        target = {'label': '收件箱', 'folder': 'inbox'}
        refs = [
            {'target': target, 'id': 'm1', 'subject': 'Hello', 'date': '2026-11-01T00:00:00Z'},
            {'target': target, 'id': 'm2', 'subject': 'Broken'},
        ]

        def fetch_raw(ref):
            if ref['id'] == 'm2':
                return {'success': False, 'error': '读取失败'}
            return {'success': True, 'raw': b'From: a@example.com\r\nSubject: Hello\r\n\r\nBody'}

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'out.zip')
            result = web_outlook_app.build_mailbox_export_zip(
                path,
                'person@example.com',
                refs,
                fetch_raw,
                truncated=True,
                max_messages=2,
                scope='mailbox',
                folder_list_fallback=True,
            )
            self.assertTrue(result['success'])
            self.assertEqual(result['exported_count'], 1)
            self.assertEqual(result['failed_count'], 1)
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                eml_name = next(name for name in names if name.endswith('.eml'))
                self.assertIn('manifest.json', names)
                self.assertIn('index.csv', names)
                self.assertIn('errors.txt', names)
                self.assertIn(b'Subject: Hello', archive.read(eml_name))
                self.assertTrue(eml_name.startswith('收件箱/20261101_'))
                manifest = json.loads(archive.read('manifest.json'))
                self.assertEqual(manifest['exported_count'], 1)
                self.assertTrue(manifest['truncated'])
                self.assertTrue(manifest['folder_list_fallback'])
                self.assertIn('未能读取完整文件夹列表', manifest['note'])
                self.assertEqual(manifest['errors'][0]['id'], 'm2')
                index_csv = archive.read('index.csv').decode('utf-8-sig')
                self.assertIn('Hello', index_csv)
                self.assertNotIn('Broken', index_csv)

    def test_zip_stops_when_cancel_is_requested(self):
        target = {'label': '收件箱', 'folder': 'inbox'}
        refs = [
            {'target': target, 'id': 'm1'},
            {'target': target, 'id': 'm2'},
        ]
        fetched = []

        def fetch_raw(ref):
            fetched.append(ref['id'])
            return {'success': True, 'raw': b'Subject: A\r\n\r\n'}

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'out.zip')
            result = web_outlook_app.build_mailbox_export_zip(
                path,
                'person@example.com',
                refs,
                fetch_raw,
                should_cancel=lambda: len(fetched) >= 1,
            )
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['exported_count'], 1)
        self.assertEqual(fetched, ['m1'])

    def test_imap_export_fetch_prefers_body_peek(self):
        queries = []

        def fake_fetch(_connection, _message_id, query, preferred_mode='uid'):
            queries.append((query, preferred_mode))
            if 'PEEK' in query:
                return 'OK', [(b'1 (BODY[] {5}', b'hello')], 'uid', []
            return 'NO', None, '', []

        account = {'email': 'imap@example.com', 'account_type': 'outlook', 'provider': 'outlook'}
        ctx = web_outlook_app.MailboxExportContext()
        with patch.object(web_outlook_app, 'select_export_imap_folder', return_value={'success': True, 'connection': object()}), \
                patch.object(web_outlook_app, 'fetch_imap_message', side_effect=fake_fetch):
            result = web_outlook_app.fetch_imap_export_raw(
                account,
                {'id': '15', 'id_mode': 'uid', 'channel': 'imap', 'target': {'folder': 'inbox', 'mailbox': ''}},
                ctx,
            )
        self.assertTrue(result['success'])
        self.assertEqual(result['raw'], b'hello')
        self.assertEqual(queries, [('(BODY.PEEK[])', 'uid')])

    def test_export_job_reports_folder_progress_while_collecting(self):
        self._insert_account('progress-export@example.com')
        target = {
            'label': '收件箱',
            'folder': 'inbox',
            'folder_id': '',
            'mailbox': '',
            'channel': 'graph',
            'well_known': 'inbox',
        }

        def list_refs(_account, folder_target, _ctx, _limit, _should_cancel=None):
            return {
                'success': True,
                'refs': [{
                    'target': folder_target,
                    'id': 'm1',
                    'channel': 'graph',
                    'subject': 'Hello',
                    'date': '2026-11-09T00:00:00Z',
                }],
                'truncated': False,
            }

        def fetch_raw(_account, _ref, _ctx):
            return {'success': True, 'raw': b'From: a@example.com\r\nSubject: Hello\r\n\r\nBody'}

        with patch.object(web_outlook_app, 'start_mailbox_export_thread', side_effect=web_outlook_app.run_mailbox_export_job), \
                patch.object(web_outlook_app, 'build_mailbox_export_targets', return_value={'success': True, 'targets': [target]}), \
                patch.object(web_outlook_app, 'list_target_export_refs', side_effect=list_refs), \
                patch.object(web_outlook_app, 'fetch_export_message_raw', side_effect=fetch_raw):
            response = self.client.post('/api/emails/progress-export@example.com/export', json={'scope': 'mailbox'})

        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertTrue(payload['success'])
        self.assertEqual(payload['status'], 'succeeded')
        self.assertEqual(payload['completed'], 1)
        self.assertEqual(payload['current_folder'], '收件箱')

    def test_export_job_downloads_zip_for_one_mailbox(self):
        self._insert_account('person-export@example.com')
        target = {'label': '收件箱', 'folder': 'inbox', 'folder_id': '', 'mailbox': '', 'channel': 'graph', 'well_known': 'inbox'}
        refs = [
            {'target': target, 'id': 'm1', 'channel': 'graph', 'subject': 'Hello', 'date': '2026-11-09T01:02:03Z'},
            {'target': target, 'id': 'm2', 'channel': 'graph', 'subject': 'Broken'},
        ]

        def fetch_raw(_account, ref, _ctx):
            if ref['id'] == 'm2':
                return {'success': False, 'error': '读取失败'}
            return {'success': True, 'raw': b'From: a@example.com\r\nSubject: Hello\r\n\r\nBody'}

        with patch.object(web_outlook_app, 'start_mailbox_export_thread', side_effect=web_outlook_app.run_mailbox_export_job), \
                patch.object(web_outlook_app, 'build_mailbox_export_targets', return_value={'success': True, 'targets': [target]}), \
                patch.object(web_outlook_app, 'collect_mailbox_export_refs', return_value={
                    'success': True,
                    'cancelled': False,
                    'error': '',
                    'refs': refs,
                    'folder_errors': [],
                    'truncated': False,
                }), \
                patch.object(web_outlook_app, 'fetch_export_message_raw', side_effect=fetch_raw):
            response = self.client.post('/api/emails/person-export@example.com/export', json={'scope': 'mailbox'})

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload['success'])
        self.assertEqual(payload['status'], 'succeeded')
        self.assertEqual(payload['completed'], 1)
        self.assertEqual(payload['failed_count'], 1)
        self.assertEqual(payload['email'], 'person-export@example.com')

        download = self.client.get(f"/api/emails/export-jobs/{payload['job_id']}/download")
        self.assertEqual(download.status_code, 200)
        self.assertIn('application/zip', download.content_type)
        self.assertIn('person-export@example.com-emails.zip', download.headers.get('Content-Disposition', ''))
        with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
            eml_name = next(name for name in archive.namelist() if name.endswith('.eml'))
            self.assertIn(b'Subject: Hello', archive.read(eml_name))
            manifest = json.loads(archive.read('manifest.json'))
        self.assertEqual(manifest['email'], 'person-export@example.com')
        self.assertEqual(manifest['failed_count'], 1)

    def test_second_export_for_same_mailbox_is_rejected_while_running(self):
        self._insert_account('busy-export@example.com')
        job_id = 'busy-export-job-123456'
        web_outlook_app.mailbox_export_jobs[job_id] = {
            'job_id': job_id,
            'status': 'running',
            'email': 'busy-export@example.com',
            'created_at': web_outlook_app.time_module_now(),
            'cancel_requested': False,
            'file_path': '',
            'filename': 'busy-export@example.com-emails.zip',
        }
        response = self.client.post('/api/emails/busy-export@example.com/export', json={'scope': 'mailbox'})
        payload = response.get_json()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(payload['job_id'], job_id)
        self.assertIn('正在导出', payload['error'])

    def test_cancel_and_incomplete_download(self):
        job_id = 'cancel-export-job-1234'
        web_outlook_app.mailbox_export_jobs[job_id] = {
            'job_id': job_id,
            'status': 'running',
            'email': 'cancel-export@example.com',
            'created_at': web_outlook_app.time_module_now(),
            'cancel_requested': False,
            'file_path': '',
            'filename': 'cancel-export@example.com-emails.zip',
            'message': '',
            'error': '',
            'total': 0,
            'completed': 0,
            'failed_count': 0,
            'truncated': False,
            'max_messages': 2000,
            'current_folder': '',
            'scope': 'mailbox',
        }
        download = self.client.get(f'/api/emails/export-jobs/{job_id}/download')
        self.assertEqual(download.status_code, 409)

        cancelled = self.client.post(f'/api/emails/export-jobs/{job_id}/cancel')
        self.assertEqual(cancelled.status_code, 200)
        self.assertTrue(cancelled.get_json()['success'])
        self.assertTrue(web_outlook_app.mailbox_export_jobs[job_id]['cancel_requested'])


class MailboxExportFrontendTests(unittest.TestCase):
    def test_single_mailbox_export_is_wired_in_the_ui(self):
        layout = pathlib.Path(ROOT_DIR, 'templates', 'partials', 'index', 'layout.html').read_text(encoding='utf-8')
        dialogs = pathlib.Path(ROOT_DIR, 'templates', 'partials', 'index', 'dialogs-primary.html').read_text(encoding='utf-8')
        emails_js = pathlib.Path(ROOT_DIR, 'static', 'js', 'index', '05-emails.js').read_text(encoding='utf-8')
        accounts_js = pathlib.Path(ROOT_DIR, 'static', 'js', 'index', '02-groups.js').read_text(encoding='utf-8')
        core_js = pathlib.Path(ROOT_DIR, 'static', 'js', 'index', '01-core.js').read_text(encoding='utf-8')

        self.assertIn('id="exportMailboxEmailsBtn"', layout)
        self.assertIn('showMailboxExportModal()', layout)
        self.assertIn('id="mailboxExportModal"', dialogs)
        self.assertIn('整个邮箱（各个文件夹）', dialogs)
        self.assertIn("data-account-action=\"exportEmails\"", accounts_js)
        self.assertIn('导出全部邮件', accounts_js)
        self.assertIn("action === 'exportEmails'", core_js)
        self.assertIn('function showMailboxExportModal(email, options = {})', emails_js)
        self.assertIn('function startMailboxExport()', emails_js)
        self.assertIn('/export', emails_js)
        self.assertIn('/api/emails/export-jobs/', emails_js)
        self.assertIn('function syncMailboxExportButton()', emails_js)
        self.assertIn('currentMethod !== \'gptmail\'', emails_js)
