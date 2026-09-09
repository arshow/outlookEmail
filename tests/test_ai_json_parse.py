import unittest
from unittest.mock import MagicMock, patch

from outlook_web.ai.llm import call_gemini
from outlook_web.ai.prompts import build_translate_email_zh_prompt
from outlook_web.ai.schema import parse_json_text
from outlook_web.ai.service import _coerce_translation_payload, translate_email_to_zh


class ParseJsonTextTests(unittest.TestCase):
    def test_parses_plain_object(self):
        payload = parse_json_text('{"subjectZh":"主题","bodyZh":"正文"}', 'bad json')
        self.assertEqual(payload['subjectZh'], '主题')
        self.assertEqual(payload['bodyZh'], '正文')

    def test_strips_markdown_fence(self):
        raw = '```json\n{"subjectZh":"主题","bodyZh":"正文"}\n```'
        payload = parse_json_text(raw, 'bad json')
        self.assertEqual(payload['bodyZh'], '正文')

    def test_extracts_object_from_prose(self):
        raw = 'Sure, here you go:\n{"subjectZh":"主题","bodyZh":"正文"}\nThanks'
        payload = parse_json_text(raw, 'bad json')
        self.assertEqual(payload['subjectZh'], '主题')

    def test_repairs_raw_newlines_inside_strings(self):
        raw = '{"subjectZh":"订单已确认","bodyZh":"亲爱的顾客\n订单 ZR#1126 已确认。"}'
        payload = parse_json_text(raw, 'gemini 返回了无效翻译 JSON')
        self.assertEqual(payload['subjectZh'], '订单已确认')
        self.assertIn('ZR#1126', payload['bodyZh'])
        self.assertIn('\n', payload['bodyZh'])

    def test_closes_truncated_object(self):
        raw = '{"subjectZh":"主题","bodyZh":"第一段还没写完'
        payload = parse_json_text(raw, 'bad json')
        self.assertEqual(payload['subjectZh'], '主题')
        self.assertTrue(payload['bodyZh'].startswith('第一段'))

    def test_invalid_text_still_raises(self):
        with self.assertRaises(ValueError) as ctx:
            parse_json_text('not-json-at-all', 'gemini 返回了无效翻译 JSON')
        self.assertEqual(str(ctx.exception), 'gemini 返回了无效翻译 JSON')


class TranslatePromptTests(unittest.TestCase):
    def test_prompt_requires_escaped_newlines(self):
        prompt = build_translate_email_zh_prompt(subject='Hello', body='World')
        self.assertNotIn('使用真实换行，不要转义', prompt)
        self.assertIn('合法 JSON', prompt)
        self.assertIn('\\n', prompt)


class CoerceTranslationPayloadTests(unittest.TestCase):
    def test_accepts_plain_chinese_text(self):
        payload = _coerce_translation_payload('订单 ZR#1126 已确认。', 'gemini')
        self.assertEqual(payload['bodyZh'], '订单 ZR#1126 已确认。')

    def test_rejects_unrecoverable_json(self):
        with self.assertRaises(ValueError):
            _coerce_translation_payload('{broken', 'gemini')


class TranslateEmailToZhRecoveryTests(unittest.TestCase):
    def test_recovers_from_unescaped_newline_json(self):
        settings = {
            'enabled': True,
            'provider': 'gemini',
            'model': 'gemini-3.7-flash',
            'gemini_api_key': 'test-key',
            'deepseek_api_key': '',
            'gemini_base_url': '',
            'deepseek_base_url': '',
            'gemini_socks5': {'enabled': False},
            'system_persona': '',
        }
        messy = '{"subjectZh":"回复：订单 ZR#1126 已确认","bodyZh":"您好\n您的订单已确认。"}'
        with patch('outlook_web.ai.service.call_structured_model', return_value=messy) as mocked:
            result = translate_email_to_zh(
                settings=settings,
                subject='Re: Order ZR#1126 confirmed',
                body='Hello, your order is confirmed.',
            )
        self.assertTrue(result['success'])
        self.assertEqual(result['subject_translation'], '回复：订单 ZR#1126 已确认')
        self.assertIn('订单已确认', result['body_translation'])
        self.assertEqual(mocked.call_args.kwargs['thinking_budget'], 0)
        self.assertGreaterEqual(mocked.call_args.kwargs['max_output_tokens'], 8192)


def _ok_gemini_response(text: str):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        'candidates': [{
            'finishReason': 'STOP',
            'content': {
                'parts': [
                    {'thought': True, 'text': 'I should return JSON.'},
                    {'text': text},
                ]
            },
        }]
    }
    return response


class GeminiResponseParsingTests(unittest.TestCase):
    def test_skips_thought_parts_and_disables_thinking(self):
        with patch('outlook_web.ai.llm.requests.post', return_value=_ok_gemini_response('{"ok":true}')) as mocked:
            text = call_gemini(
                api_key='key',
                model='gemini-2.5-flash',
                prompt='hi',
                temperature=0,
                max_output_tokens=64,
                thinking_budget=0,
            )
        self.assertEqual(text, '{"ok":true}')
        body = mocked.call_args.kwargs['json']
        config = body['generationConfig']
        self.assertEqual(config['thinkingConfig']['thinkingBudget'], 0)
        self.assertEqual(config['temperature'], 0)
        self.assertNotIn('thinkingLevel', config['thinkingConfig'])

    def test_gemini_3_7_flash_uses_thinking_level_low_without_temperature(self):
        with patch('outlook_web.ai.llm.requests.post', return_value=_ok_gemini_response('{"ok":true}')) as mocked:
            text = call_gemini(
                api_key='key',
                model='gemini-3.7-flash',
                prompt='hi',
                temperature=0.1,
                max_output_tokens=8192,
                thinking_budget=0,
            )
        self.assertEqual(text, '{"ok":true}')
        config = mocked.call_args.kwargs['json']['generationConfig']
        self.assertEqual(config['thinkingConfig'], {'thinkingLevel': 'low'})
        self.assertNotIn('temperature', config)
        self.assertNotIn('thinkingBudget', config['thinkingConfig'])


class EmbeddedMediaSanitizeTests(unittest.TestCase):
    def test_strips_inline_image_and_keeps_visible_text(self):
        from outlook_web.ai.context import html_to_text, normalize_email_detail, strip_embedded_media

        blob = 'A' * 500
        html = (
            '<p>Bonjour merci pour votre réponse</p>'
            f'<img src="data:image/jpeg;base64,{blob}" alt="order">'
        )
        cleaned = strip_embedded_media(html)
        self.assertIn('Bonjour', cleaned)
        self.assertIn('[图片]', cleaned)
        self.assertNotIn(blob[:40], cleaned)
        self.assertNotIn(blob[:40], html_to_text(html))

        detail = normalize_email_detail({
            'subject': 'Re: coffee',
            'from': 'ana@example.com',
            'body': html,
            'body_type': 'html',
            'attachments': [{'name': 'order.jpg'}],
        })
        self.assertIn('Bonjour', detail['body_text'])
        self.assertIn('[附件: order.jpg]', detail['body_text'])
        self.assertNotIn(blob[:40], detail['body_text'])


if __name__ == '__main__':
    unittest.main()
