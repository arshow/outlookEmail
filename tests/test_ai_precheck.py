import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-ai-precheck-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')
if 'SECRET_KEY' not in os.environ:
    os.environ['SECRET_KEY'] = 'test-secret-key'

import web_outlook_app
from outlook_web.ai.constants import (
    PRECHECK_DECISION_CUSTOMER_ANALYZED,
    PRECHECK_DECISION_FAILED,
    PRECHECK_DECISION_MISSING_CLEF,
    PRECHECK_DECISION_NOT_CUSTOMER,
    PRECHECK_DECISION_SKIPPED_NOTIFICATION,
)
from outlook_web.ai.precheck import (
    classify_fixed_notification,
    collect_new_inbox_emails,
    extract_noul_probability,
    process_inbox_precheck_item,
)


ROOT_DIR = os.path.dirname(os.path.dirname(__file__))

SHOPIFY_NOTICE = {
    'id': 'msg-shopify-notice',
    'id_mode': 'graph',
    'from': 'store@t.shopifyemail.com',
    'subject': 'Order #1001 confirmed',
    'body': 'Your Shopify order has been confirmed.',
    'body_preview': 'Your Shopify order has been confirmed.',
}

SHOPIFY_CONTACT_FORM = {
    'id': 'msg-shopify-form',
    'id_mode': 'graph',
    'from': 'store@email.shopify.com',
    'subject': "You received a new message from your online store's contact form",
    'body': (
        "You received a new message from your online store's contact form\n"
        'Name: Jane\n'
        'Email: jane@example.com\n'
        'I need help with my order.'
    ),
    'body_preview': 'I need help with my order.',
}

CUSTOMER_MAIL = {
    'id': 'msg-customer-1',
    'id_mode': 'graph',
    'from': 'jane@example.com',
    'subject': 'Where is my order?',
    'body': 'Hi, I still have not received my package.',
    'body_preview': 'Hi, I still have not received my package.',
}

CLEF_AND_GEN_SETTINGS = {
    'enabled': True,
    'provider': 'deepseek',
    'deepseek_api_key': 'ds-secret',
    'cloudflare_account_id': 'acc-123',
    'cloudflare_api_token': 'cf-token',
    'cloudflare_model': 'clef-flash',
}


class PrecheckHelpersTestCase(unittest.TestCase):
    def test_classify_shopify_notification_and_contact_form(self):
        self.assertEqual(
            classify_fixed_notification(SHOPIFY_NOTICE),
            PRECHECK_DECISION_SKIPPED_NOTIFICATION,
        )
        self.assertEqual(
            classify_fixed_notification({
                **SHOPIFY_NOTICE,
                'from': 'noreply@shopify.com',
            }),
            PRECHECK_DECISION_SKIPPED_NOTIFICATION,
        )
        self.assertIsNone(classify_fixed_notification(SHOPIFY_CONTACT_FORM))
        self.assertIsNone(classify_fixed_notification(CUSTOMER_MAIL))

    def test_extract_noul_probability(self):
        self.assertEqual(extract_noul_probability(0.73), 0.73)
        self.assertEqual(extract_noul_probability(True), 1.0)
        self.assertEqual(extract_noul_probability(False), 0.0)
        self.assertEqual(extract_noul_probability({'probability': 0.61}), 0.61)
        self.assertEqual(extract_noul_probability({'choice': 'yes'}), 1.0)
        self.assertEqual(extract_noul_probability({'choice': 'no'}), 0.0)
        self.assertIsNone(extract_noul_probability({}))

    def test_collect_new_inbox_emails_matches_identifier_dicts(self):
        emails = [CUSTOMER_MAIL, SHOPIFY_NOTICE]
        matched = collect_new_inbox_emails(
            emails,
            [{'id': 'msg-customer-1', 'folder': 'inbox', 'id_mode': 'graph'}],
        )
        self.assertEqual([item['id'] for item in matched], ['msg-customer-1'])
        self.assertEqual(collect_new_inbox_emails(emails, []), [])


class AiPrecheckPipelineTestCase(unittest.TestCase):
    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess['logged_in'] = True

        with self.app.app_context():
            web_outlook_app.init_db()
            db = web_outlook_app.get_db()
            db.execute('DELETE FROM ai_precheck_runs')
            db.execute('DELETE FROM ai_drafts')
            db.execute('DELETE FROM ai_analysis_runs')
            db.execute('DELETE FROM retained_normal_mail_messages')
            db.execute("DELETE FROM accounts WHERE email = ?", ('precheck@example.com',))
            db.commit()
            self.assertTrue(web_outlook_app.add_account(
                'precheck@example.com',
                'password',
                'client-id',
                'refresh-token',
                group_id=1,
                account_type='outlook',
                provider='outlook',
            ))
            self.account = web_outlook_app.get_account_by_email('precheck@example.com')

    def _run(self, email_item, settings=None, **kwargs):
        with self.app.app_context():
            return process_inbox_precheck_item(
                db=web_outlook_app.get_db(),
                settings=settings or CLEF_AND_GEN_SETTINGS,
                account=self.account,
                email_item=email_item,
                folder='inbox',
                **kwargs,
            )

    def _run_row(self, message_id):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            return db.execute(
                '''
                SELECT decision, analyze_run_id, error_message, clef_answers
                FROM ai_precheck_runs
                WHERE account_id = ? AND folder = 'inbox' AND message_id = ?
                ''',
                (self.account['id'], message_id),
            ).fetchone()

    def test_new_account_defaults_precheck_off(self):
        self.assertFalse(bool(self.account.get('ai_precheck_enabled')))
        with self.app.app_context():
            db = web_outlook_app.get_db()
            row = db.execute(
                'SELECT ai_precheck_enabled FROM accounts WHERE id = ?',
                (self.account['id'],),
            ).fetchone()
        self.assertEqual(int(row['ai_precheck_enabled']), 0)

    def test_shopify_notification_skips_clef_and_analyze(self):
        call_clef_fn = Mock()
        analyze_fn = Mock()
        result = self._run(
            SHOPIFY_NOTICE,
            call_clef_fn=call_clef_fn,
            analyze_fn=analyze_fn,
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['decision'], PRECHECK_DECISION_SKIPPED_NOTIFICATION)
        call_clef_fn.assert_not_called()
        analyze_fn.assert_not_called()
        row = self._run_row('msg-shopify-notice')
        self.assertEqual(row['decision'], PRECHECK_DECISION_SKIPPED_NOTIFICATION)

    def test_shopify_contact_form_goes_to_clef(self):
        analyze_fn = Mock()
        result = self._run(
            SHOPIFY_CONTACT_FORM,
            call_clef_fn=lambda **kwargs: {'answers': {'is_customer': 0.2}},
            analyze_fn=analyze_fn,
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['decision'], PRECHECK_DECISION_NOT_CUSTOMER)
        analyze_fn.assert_not_called()

    def test_clef_not_customer_does_not_analyze(self):
        analyze_fn = Mock()
        result = self._run(
            CUSTOMER_MAIL,
            call_clef_fn=lambda **kwargs: {'answers': {'is_customer': {'probability': 0.59}}},
            analyze_fn=analyze_fn,
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['decision'], PRECHECK_DECISION_NOT_CUSTOMER)
        analyze_fn.assert_not_called()

    def test_clef_customer_analyzes_once(self):
        analyze_fn = Mock(return_value={'run_id': 77})
        result = self._run(
            CUSTOMER_MAIL,
            call_clef_fn=lambda **kwargs: {'answers': {'is_customer': 0.6}},
            analyze_fn=analyze_fn,
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['decision'], PRECHECK_DECISION_CUSTOMER_ANALYZED)
        self.assertEqual(result['run_id'], 77)
        analyze_fn.assert_called_once()
        kwargs = analyze_fn.call_args.kwargs
        self.assertEqual(kwargs['context_scope'], 'current')
        self.assertEqual(kwargs['message_id'], 'msg-customer-1')
        self.assertEqual(kwargs['current_detail']['body'], CUSTOMER_MAIL['body'])

        duplicate = self._run(
            CUSTOMER_MAIL,
            call_clef_fn=lambda **kwargs: {'answers': {'is_customer': 0.99}},
            analyze_fn=analyze_fn,
        )
        self.assertTrue(duplicate['skipped'])
        self.assertEqual(duplicate['reason'], 'already_processed')
        analyze_fn.assert_called_once()
        row = self._run_row('msg-customer-1')
        self.assertEqual(row['decision'], PRECHECK_DECISION_CUSTOMER_ANALYZED)
        self.assertEqual(int(row['analyze_run_id']), 77)

    def test_missing_clef_does_not_call_generate(self):
        analyze_fn = Mock()
        result = self._run(
            CUSTOMER_MAIL,
            settings={'enabled': True, 'provider': 'deepseek', 'deepseek_api_key': 'ds-secret'},
            call_clef_fn=lambda **kwargs: {'answers': {'is_customer': 0.9}},
            analyze_fn=analyze_fn,
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['decision'], PRECHECK_DECISION_MISSING_CLEF)
        analyze_fn.assert_not_called()

    def test_customer_without_generation_model_fails(self):
        result = self._run(
            CUSTOMER_MAIL,
            settings={
                'enabled': False,
                'cloudflare_account_id': 'acc-123',
                'cloudflare_api_token': 'cf-token',
                'cloudflare_model': 'clef-flash',
            },
            call_clef_fn=lambda **kwargs: {'answers': {'is_customer': 0.9}},
            analyze_fn=lambda **kwargs: {'_': 1},
        )
        self.assertFalse(result['success'])
        self.assertEqual(result['decision'], PRECHECK_DECISION_FAILED)
        row = self._run_row('msg-customer-1')
        self.assertIn('未启用', row['error_message'] or '')

    def test_enqueue_respects_flag_and_folder(self):
        emails = [CUSTOMER_MAIL]
        with patch.object(web_outlook_app, '_ai_precheck_executor') as executor:
            disabled = web_outlook_app.enqueue_inbox_mail_precheck(
                self.account, emails, ['msg-customer-1'], folder='inbox'
            )
            self.assertEqual(disabled, 0)
            executor.submit.assert_not_called()

            enabled_account = dict(self.account)
            enabled_account['ai_precheck_enabled'] = True
            deleted = web_outlook_app.enqueue_inbox_mail_precheck(
                enabled_account, emails, ['msg-customer-1'], folder='deleteditems'
            )
            self.assertEqual(deleted, 0)
            executor.submit.assert_not_called()

            queued = web_outlook_app.enqueue_inbox_mail_precheck(
                enabled_account, emails, [{'id': 'msg-customer-1'}], folder='inbox'
            )
            self.assertEqual(queued, 1)
            junk = web_outlook_app.enqueue_inbox_mail_precheck(
                enabled_account, emails, ['msg-customer-1'], folder='junkemail'
            )
            self.assertEqual(junk, 1)
            self.assertEqual(executor.submit.call_count, 2)

    def _inbox_fetch_payload(self):
        return {
            'success': True,
            'emails': [{
                **CUSTOMER_MAIL,
                'to': 'precheck@example.com',
                'date': '2026-10-14T12:00:00Z',
                'is_read': False,
                'is_flagged': False,
                'has_attachments': False,
                'folder': 'inbox',
            }],
            'method': 'Graph API',
            'has_more': False,
        }

    def _discovery_fetch(self, account, folder, skip, top, **kwargs):
        if folder == 'junkemail':
            return {'success': True, 'emails': [], 'method': 'Graph API', 'has_more': False}
        return self._inbox_fetch_payload()

    def test_discover_skips_enqueue_when_flag_off_and_keeps_body_when_on(self):
        with self.app.app_context():
            web_outlook_app.set_setting('normal_mail_local_retention_enabled', 'true')
            if hasattr(web_outlook_app, 'clear_normal_mail_local_retention_enabled_cache'):
                web_outlook_app.clear_normal_mail_local_retention_enabled_cache()

            with patch.object(
                web_outlook_app,
                'fetch_account_folder_emails',
                side_effect=self._discovery_fetch,
            ), patch.object(web_outlook_app, '_ai_precheck_executor') as executor:
                web_outlook_app.discover_account_inbox_mail(self.account, top=20)
            executor.submit.assert_not_called()

            db = web_outlook_app.get_db()
            db.execute(
                'UPDATE accounts SET ai_precheck_enabled = 1 WHERE id = ?',
                (self.account['id'],),
            )
            db.commit()
            enabled = web_outlook_app.get_account_by_email('precheck@example.com')

            captured = {}

            def capture_enqueue(account, emails, new_message_ids, folder='inbox'):
                captured['body'] = (emails[0] or {}).get('body')
                captured['folder'] = folder
                captured['ids'] = list(new_message_ids)
                return 1

            db.execute('DELETE FROM retained_normal_mail_messages')
            db.commit()
            with patch.object(
                web_outlook_app,
                'fetch_account_folder_emails',
                side_effect=self._discovery_fetch,
            ), patch.object(
                web_outlook_app, 'enqueue_inbox_mail_precheck', side_effect=capture_enqueue
            ) as enqueue:
                result = web_outlook_app.discover_account_inbox_mail(enabled, top=20)

        self.assertTrue(result['success'])
        self.assertEqual(result['new_count'], 1)
        enqueue.assert_called_once()
        self.assertEqual(captured['body'], CUSTOMER_MAIL['body'])
        self.assertEqual(captured['folder'], 'inbox')

    def test_batch_and_put_account_flag(self):
        response = self.client.post('/api/accounts/batch-update-ai-precheck', json={
            'account_ids': [self.account['id']],
            'ai_precheck_enabled': True,
        })
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['updated_count'], 1)

        with self.app.app_context():
            account = web_outlook_app.get_account_by_email('precheck@example.com')
        self.assertTrue(bool(account.get('ai_precheck_enabled')))

        detail = self.client.get(f'/api/accounts/{self.account["id"]}').get_json()
        self.assertTrue(detail['success'])
        self.assertTrue(detail['account']['ai_precheck_enabled'])

        put = self.client.put(f'/api/accounts/{self.account["id"]}', json={
            'email': account['email'],
            'client_id': account['client_id'],
            'refresh_token': account['refresh_token'],
            'account_type': 'outlook',
            'provider': 'outlook',
            'group_id': account['group_id'],
            'remark': account.get('remark') or '',
            'status': 'active',
            'forward_enabled': False,
            'inbox_poll_enabled': True,
            'aggregated_inbox_enabled': False,
            'ai_precheck_enabled': False,
        })
        self.assertTrue(put.get_json()['success'])
        with self.app.app_context():
            updated = web_outlook_app.get_account_by_email('precheck@example.com')
        self.assertFalse(bool(updated.get('ai_precheck_enabled')))


class AiPrecheckFrontendTestCase(unittest.TestCase):
    def test_account_list_edit_and_batch_controls(self):
        groups_js = Path(ROOT_DIR, 'static', 'js', 'index', '02-groups.js').read_text(encoding='utf-8')
        settings_js = Path(ROOT_DIR, 'static', 'js', 'index', '07-settings.js').read_text(encoding='utf-8')
        batch_js = Path(ROOT_DIR, 'static', 'js', 'index', '10-batch-actions.js').read_text(encoding='utf-8')
        css = Path(ROOT_DIR, 'static', 'css', 'index', '04-account-panel.css').read_text(encoding='utf-8')
        layout_html = Path(ROOT_DIR, 'templates', 'partials', 'index', 'layout.html').read_text(encoding='utf-8')
        primary_html = Path(
            ROOT_DIR, 'templates', 'partials', 'index', 'dialogs-primary.html'
        ).read_text(encoding='utf-8')

        self.assertIn('showAiPrecheckStatusLabel', groups_js)
        self.assertIn('data-ai-precheck-enabled', groups_js)
        self.assertIn('已开启前置判断', groups_js)
        self.assertIn('.account-status-pill.precheck', css)
        self.assertIn('editAiPrecheckEnabled', primary_html)
        self.assertIn('editAiPrecheckEnabled', settings_js)
        self.assertIn('ai_precheck_enabled', settings_js)
        self.assertIn('batchEnableAiPrecheckBtn', layout_html)
        self.assertIn('batchDisableAiPrecheckBtn', layout_html)
        self.assertIn('/api/accounts/batch-update-ai-precheck', batch_js)
        self.assertIn('enableAiPrecheckForSelectedAccounts', batch_js)
        self.assertIn('disableAiPrecheckForSelectedAccounts', batch_js)


if __name__ == '__main__':
    unittest.main()
