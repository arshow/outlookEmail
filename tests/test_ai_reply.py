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
            db.execute('DELETE FROM retained_normal_mail_messages')
            db.execute(
                "DELETE FROM accounts WHERE email IN (?, ?)",
                ('user@example.com', 'user2@example.com'),
            )
            db.execute('UPDATE ai_knowledge_meta SET revision = 1 WHERE id = 1')
            for key in (
                'ai_reply_enabled',
                'ai_reply_provider',
                'ai_reply_model',
                'ai_reply_gemini_api_key',
                'ai_reply_deepseek_api_key',
                'ai_reply_gemini_remark',
                'ai_reply_deepseek_remark',
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
        self.assertEqual(settings['deepseek_api_key'], 'ds-secret')
        self.assertEqual(settings['gemini_api_key'], 'gm-secret')

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

        remarks = self.client.put('/api/ai/settings', json={
            'gemini_remark': '主 Key / gemini-3.8-flash',
            'deepseek_remark': '备用',
        })
        remark_settings = remarks.get_json()['settings']
        self.assertEqual(remark_settings['gemini_remark'], '主 Key / gemini-3.8-flash')
        self.assertEqual(remark_settings['deepseek_remark'], '备用')

    def test_list_available_models_endpoint(self):
        self.client.put('/api/ai/settings', json={
            'provider': 'gemini',
            'gemini_api_key': 'gm-secret',
        })

        with patch('outlook_web.ai.llm.list_gemini_models') as mock_list:
            mock_list.return_value = [
                {'id': 'gemini-2.5-flash', 'display_name': 'Gemini 2.5 Flash', 'description': ''},
                {'id': 'gemini-2.0-flash', 'display_name': 'Gemini 2.0 Flash', 'description': ''},
            ]
            response = self.client.post('/api/ai/settings/models', json={'provider': 'gemini'})
        payload = response.get_json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['provider'], 'gemini')
        self.assertEqual(payload['count'], 2)
        self.assertEqual(payload['models'][0]['id'], 'gemini-2.5-flash')
        mock_list.assert_called_once()
        self.assertEqual(mock_list.call_args.kwargs.get('api_key'), 'gm-secret')

        missing = self.client.post('/api/ai/settings/models', json={
            'provider': 'deepseek',
            'deepseek_api_key': '',
        })
        missing_payload = missing.get_json()
        self.assertFalse(missing_payload['success'])
        self.assertIn('DeepSeek API Key', missing_payload.get('error') or '')

    def test_map_gemini_error_messages(self):
        from outlook_web.ai.llm import _map_gemini_error

        high_demand = _map_gemini_error(
            'This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.'
        )
        self.assertIn('请求量过高', high_demand)
        self.assertIn('gemini-2.5-flash', high_demand)

        quota = _map_gemini_error(
            'You exceeded your current quota, please check your plan and billing details. '
            'Metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, Limit: 20, '
            'Model: gemini-3.7-flash. Please retry in 58.454115043s.'
        )
        self.assertIn('配额', quota)
        self.assertIn('gemini-3.7-flash', quota)
        self.assertIn('58 秒', quota)
        self.assertIn('20', quota)

    def test_list_gemini_and_deepseek_model_parsers(self):
        from outlook_web.ai import llm as ai_llm

        class FakeResponse:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        with patch('outlook_web.ai.llm.requests.get') as mock_get:
            mock_get.return_value = FakeResponse(200, {
                'models': [
                    {
                        'name': 'models/gemini-2.5-flash',
                        'displayName': 'Gemini 2.5 Flash',
                        'supportedGenerationMethods': ['generateContent'],
                    },
                    {
                        'name': 'models/embedding-001',
                        'displayName': 'Embedding',
                        'supportedGenerationMethods': ['embedContent'],
                    },
                ],
            })
            gemini_models = ai_llm.list_gemini_models(api_key='k', base_url='https://example.invalid')
        self.assertEqual([m['id'] for m in gemini_models], ['gemini-2.5-flash'])

        with patch('outlook_web.ai.llm.requests.get') as mock_get:
            mock_get.return_value = FakeResponse(200, {
                'data': [
                    {'id': 'deepseek-reasoner', 'owned_by': 'deepseek'},
                    {'id': 'deepseek-chat', 'owned_by': 'deepseek'},
                ],
            })
            deepseek_models = ai_llm.list_deepseek_models(api_key='k', base_url='https://example.invalid')
        self.assertEqual([m['id'] for m in deepseek_models], ['deepseek-chat', 'deepseek-reasoner'])

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

        with patch.object(
            web_outlook_app,
            'fetch_email_detail_for_account',
            side_effect=AssertionError('AI analyze must not remote-fetch email detail'),
        ), patch('outlook_web.ai.service.call_structured_model', return_value=json.dumps(analysis)):
            response = self.client.post('/api/ai/analyze', json={
                'email': 'user@example.com',
                'message_id': 'msg-1',
                'context_scope': 'current',
                'email_detail': {
                    'id': 'msg-1',
                    'subject': 'Hello',
                    'from': 'customer@example.com',
                    'body': 'Any update?',
                    'body_type': 'text',
                },
            })

        payload = response.get_json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload.get('detail_source'), 'client')
        self.assertEqual(payload['analysis']['replyText'], analysis['replyText'])
        self.assertEqual(payload['meta']['context_scope'], 'current')

        status = self.client.get('/api/ai/status').get_json()
        self.assertTrue(status['ready'])

    def test_analyze_uses_local_retention_and_never_remote_fetch(self):
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
                ('user2@example.com', 'cid', 'token'),
            )
            account_id = db.execute(
                "SELECT id FROM accounts WHERE email = ?",
                ('user2@example.com',),
            ).fetchone()['id']
            db.execute(
                '''
                INSERT INTO retained_normal_mail_messages (
                    account_id, folder, provider_message_id, id_mode, subject, sender,
                    recipients, received_at, received_at_sort, body, body_type, body_preview,
                    body_cached, list_cached
                ) VALUES (?, 'inbox', ?, '', ?, ?, ?, ?, 1, ?, 'text', ?, 1, 1)
                ''',
                (
                    account_id,
                    'msg-local-1',
                    'Need update',
                    'customer@example.com',
                    'user2@example.com',
                    '2026-01-01 10:00:00',
                    'Any update on my order?',
                    'Any update on my order?',
                ),
            )
            db.commit()

        analysis = {
            'sourceLanguage': 'en',
            'summaryZh': '客户询问更新',
            'intent': 'general_question',
            'riskLevel': 'green',
            'riskReasons': [],
            'matchedRuleIds': [],
            'matchedKnowledgeIds': [],
            'missingFacts': [],
            'internalAdviceZh': '',
            'replyLanguage': 'en',
            'replyText': 'Thanks for the update request.',
            'replyTextZh': '感谢您的更新询问。',
            'confidence': 0.7,
            'requiresHumanConfirmation': False,
        }

        with patch.object(
            web_outlook_app,
            'fetch_email_detail_for_account',
            side_effect=AssertionError('AI analyze must not remote-fetch email detail'),
        ), patch('outlook_web.ai.service.call_structured_model', return_value=json.dumps(analysis)):
            response = self.client.post('/api/ai/analyze', json={
                'email': 'user2@example.com',
                'message_id': 'msg-local-1',
                'context_scope': 'current',
            })

        payload = response.get_json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload.get('detail_source'), 'local')
        self.assertEqual(payload['analysis']['replyText'], analysis['replyText'])

    def test_ai_translate_requires_enabled(self):
        response = self.client.post('/api/ai/translate', json={
            'subject': 'Hello',
            'text': 'How are you?',
        })
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertIn('AI', payload.get('error') or '')

    def test_ai_translate_success_with_mocked_llm(self):
        self.client.put('/api/ai/settings', json={
            'enabled': True,
            'provider': 'gemini',
            'model': 'gemini-2.5-flash',
            'gemini_api_key': 'gm-test-key',
        })
        translated = {
            'subjectZh': '你好',
            'bodyZh': '你好吗？',
        }
        with patch('outlook_web.ai.service.call_structured_model', return_value=json.dumps(translated)):
            response = self.client.post('/api/ai/translate', json={
                'subject': 'Hello',
                'text': 'How are you?',
            })
        payload = response.get_json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['provider'], 'gemini')
        self.assertEqual(payload['subject_translation'], '你好')
        self.assertEqual(payload['body_translation'], '你好吗？')
        self.assertEqual(payload['translation'], '你好\n\n你好吗？')


if __name__ == '__main__':
    unittest.main()
