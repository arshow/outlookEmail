import json
import os
import tempfile
import unittest
from unittest.mock import patch


if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-ai-reply-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')
if 'SECRET_KEY' not in os.environ:
    os.environ['SECRET_KEY'] = 'test-secret-key'

import web_outlook_app


class AiReplyTestCase(unittest.TestCase):
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
            db.execute('DELETE FROM ai_drafts')
            db.execute('DELETE FROM ai_analysis_runs')
            db.execute('DELETE FROM ai_knowledge_entries')
            db.execute('DELETE FROM ai_rule_versions')
            db.execute('UPDATE ai_knowledge_meta SET revision = 1 WHERE id = 1')
            for key in (
                'ai_reply_enabled',
                'ai_reply_provider',
                'ai_reply_model',
                'ai_reply_gemini_api_key',
                'ai_reply_deepseek_api_key',
                'ai_reply_gemini_base_url',
                'ai_reply_deepseek_base_url',
                'ai_reply_gemini_socks5',
                'ai_reply_system_persona',
            ):
                db.execute("UPDATE settings SET value = '' WHERE key = ?", (key,))
            db.execute("UPDATE settings SET value = 'false' WHERE key = 'ai_reply_enabled'")
            db.execute("UPDATE settings SET value = 'gemini' WHERE key = 'ai_reply_provider'")
            db.execute("UPDATE settings SET value = 'gemini-2.5-flash' WHERE key = 'ai_reply_model'")
            db.commit()

    def test_ai_admin_page_requires_login_and_renders(self):
        anon = self.app.test_client()
        response = anon.get('/ai')
        self.assertIn(response.status_code, (302, 401))

        response = self.client.get('/ai')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'AI', response.data)

    def test_settings_encrypt_mask_preserve_and_provider_switch(self):
        response = self.client.put('/api/ai/settings', json={
            'enabled': True,
            'provider': 'deepseek',
            'model': 'deepseek-chat',
            'deepseek_api_key': 'ds-secret',
            'gemini_api_key': 'gm-secret',
        })
        payload = response.get_json()
        self.assertTrue(payload['success'], payload)
        settings = payload['settings']
        self.assertTrue(settings['enabled'])
        self.assertEqual(settings['provider'], 'deepseek')
        self.assertTrue(settings['deepseek_api_key_configured'])
        self.assertTrue(settings['gemini_api_key_configured'])
        self.assertEqual(settings['deepseek_api_key_masked'], '********')
        self.assertNotIn('deepseek_api_key', settings)

        with self.app.app_context():
            raw = web_outlook_app.get_setting('ai_reply_deepseek_api_key')
            self.assertNotEqual(raw, 'ds-secret')
            self.assertEqual(web_outlook_app.get_setting_decrypted('ai_reply_deepseek_api_key'), 'ds-secret')

        preserve = self.client.put('/api/ai/settings', json={
            'provider': 'gemini',
            'deepseek_api_key': '',
        })
        self.assertTrue(preserve.get_json()['success'])
        with self.app.app_context():
            self.assertEqual(web_outlook_app.get_setting_decrypted('ai_reply_deepseek_api_key'), 'ds-secret')

        clear = self.client.put('/api/ai/settings', json={'clear_deepseek_api_key': True})
        self.assertTrue(clear.get_json()['success'])
        with self.app.app_context():
            self.assertEqual(web_outlook_app.get_setting_decrypted('ai_reply_deepseek_api_key'), '')

    def test_knowledge_and_rules_crud(self):
        create_k = self.client.post('/api/ai/knowledge', json={
            'title': '退款政策',
            'content': '未发货可退款，需人工确认订单状态。',
            'keywords': ['退款', 'refund'],
            'priority': 10,
        })
        self.assertTrue(create_k.get_json()['success'], create_k.get_json())
        entry_id = create_k.get_json()['entry']['id']

        listed = self.client.get('/api/ai/knowledge').get_json()
        self.assertEqual(len(listed['entries']), 1)

        updated = self.client.put(f'/api/ai/knowledge/{entry_id}', json={
            'title': '退款政策',
            'content': '更新后的内容',
            'keywords': ['退款'],
            'enabled': False,
        })
        self.assertTrue(updated.get_json()['success'])
        self.assertFalse(updated.get_json()['entry']['enabled'])

        create_r = self.client.post('/api/ai/rules', json={
            'version_label': 'refund-careful',
            'instruction': '遇到退款先核对订单，不要直接承诺。',
            'keywords': ['退款', 'refund'],
            'forbidden_phrases': ['已经退款'],
            'risk_level': 'yellow',
            'status': 'published',
        })
        self.assertTrue(create_r.get_json()['success'], create_r.get_json())

        test = self.client.post('/api/ai/rules/test', json={'text': '我想退款'})
        test_payload = test.get_json()
        self.assertTrue(test_payload['success'])
        self.assertGreaterEqual(len(test_payload['matched_rules']), 1)

        deleted = self.client.delete(f'/api/ai/knowledge/{entry_id}')
        self.assertTrue(deleted.get_json()['success'])

    def test_analyze_requires_enabled_and_key(self):
        response = self.client.post('/api/ai/analyze', json={
            'email': 'a@example.com',
            'message_id': 'mid-1',
            'context_scope': 'current',
        })
        payload = response.get_json()
        self.assertFalse(payload['success'])

    def test_output_guards_replace_forbidden_commitment(self):
        from outlook_web.ai.rules import apply_output_guards

        guarded = apply_output_guards(
            {
                'replyText': 'Your full refund has been approved already. 已经批准退款',
                'replyTextZh': '已批准退款',
                'riskLevel': 'green',
                'riskReasons': [],
                'matchedRuleIds': [],
                'matchedKnowledgeIds': [],
                'missingFacts': ['orderStatus'],
                'internalAdviceZh': '',
                'intent': 'refund',
                'requiresHumanConfirmation': False,
                'summaryZh': '退款',
                'sourceLanguage': 'en',
                'replyLanguage': 'en',
                'confidence': 0.9,
            },
            source_text='I want a refund',
            matched_rules=[{
                'id': 1,
                'forbidden_phrases': ['approved'],
                'risk_level': 'yellow',
            }],
        )
        self.assertIn('check the details first', guarded['replyText'].lower())
        self.assertEqual(guarded['riskLevel'], 'yellow')
        self.assertTrue(guarded['requiresHumanConfirmation'])

    def test_analyze_success_with_mocked_llm(self):
        self.client.put('/api/ai/settings', json={
            'enabled': True,
            'provider': 'deepseek',
            'model': 'deepseek-chat',
            'deepseek_api_key': 'ds-secret',
        })

        with self.app.app_context():
            db = web_outlook_app.get_db()
            db.execute(
                '''
                INSERT INTO accounts (email, client_id, refresh_token, account_type, status)
                VALUES (?, ?, ?, 'outlook', 'active')
                ''',
                ('user@example.com', 'cid', 'token'),
            )
            db.commit()

        analysis = {
            'sourceLanguage': 'en',
            'summaryZh': '客户询问状态',
            'intent': 'general_question',
            'riskLevel': 'green',
            'riskReasons': [],
            'matchedRuleIds': [],
            'matchedKnowledgeIds': [],
            'missingFacts': [],
            'internalAdviceZh': '直接回复',
            'replyLanguage': 'en',
            'replyText': 'Thanks, we will check and reply.',
            'replyTextZh': '感谢，我们会核实后回复。',
            'confidence': 0.8,
            'requiresHumanConfirmation': False,
        }

        def fake_fetch(account, message_id, method='graph', folder='inbox', id_mode='', prefer_local=False):
            return {
                'success': True,
                'email': {
                    'id': message_id,
                    'subject': 'Hello',
                    'from': 'customer@example.com',
                    'body': 'Any update?',
                    'body_type': 'text',
                },
            }

        with patch.object(web_outlook_app, 'fetch_email_detail_for_account', side_effect=fake_fetch), \
             patch('outlook_web.ai.service.call_structured_model', return_value=json.dumps(analysis)):
            response = self.client.post('/api/ai/analyze', json={
                'email': 'user@example.com',
                'message_id': 'msg-1',
                'context_scope': 'current',
            })

        payload = response.get_json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['analysis']['replyText'], analysis['replyText'])
        self.assertEqual(payload['meta']['context_scope'], 'current')

        status = self.client.get('/api/ai/status').get_json()
        self.assertTrue(status['ready'])


if __name__ == '__main__':
    unittest.main()
