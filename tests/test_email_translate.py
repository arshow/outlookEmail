import importlib
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    _temp_dir = tempfile.mkdtemp(prefix='outlookEmail-translate-tests-')
    os.environ['DATABASE_PATH'] = os.path.join(_temp_dir, 'test.db')
ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from outlook_web.translate_mymemory import (
    TranslateError,
    chunk_text,
    prepare_translate_fields,
    translate_email_fields_to_zh,
    translate_to_zh,
)

web_outlook_app = importlib.import_module('web_outlook_app')


class ChunkTextTests(unittest.TestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(chunk_text(''), [])
        self.assertEqual(chunk_text(None), [])

    def test_short_text_single_chunk(self):
        self.assertEqual(chunk_text('Hello world'), ['Hello world'])

    def test_splits_on_sentence_boundary(self):
        text = 'First sentence. Second sentence is here. Third one follows.'
        chunks = chunk_text(text, max_chars=30)
        self.assertTrue(all(len(chunk) <= 30 for chunk in chunks))
        self.assertEqual(''.join(chunks), text)

    def test_hard_split_without_punctuation(self):
        text = 'a' * 100
        chunks = chunk_text(text, max_chars=40)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(''.join(chunks), text)


class TranslateMyMemoryTests(unittest.TestCase):
    def test_prepare_fields_requires_content(self):
        with self.assertRaises(TranslateError):
            prepare_translate_fields(text='', html='', subject='', html_to_plain=lambda x: x)

    def test_prepare_fields_from_html(self):
        fields = prepare_translate_fields(
            text='',
            html='<p>Hello</p>',
            subject='Subject',
            html_to_plain=web_outlook_app.html_to_plain_text,
        )
        self.assertEqual(fields['subject'], 'Subject')
        self.assertEqual(fields['body'], 'Hello')

    def test_translate_to_zh_joins_chunks(self):
        session = MagicMock()
        responses = []
        for translated in ('你好', '世界'):
            response = MagicMock()
            response.status_code = 200
            response.json.return_value = {
                'responseStatus': 200,
                'responseData': {'translatedText': translated},
            }
            responses.append(response)
        session.get.side_effect = responses

        result = translate_to_zh(
            'Hello. World.',
            source_lang='en',
            max_chunk_chars=8,
            session=session,
        )
        self.assertEqual(result['provider'], 'mymemory')
        self.assertEqual(result['translation'], '你好世界')
        self.assertGreaterEqual(session.get.call_count, 2)

    def test_translate_quota_error(self):
        session = MagicMock()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            'responseStatus': 429,
            'responseDetails': 'Quota exceeded',
            'quotaFinished': True,
        }
        session.get.return_value = response
        with self.assertRaises(TranslateError) as ctx:
            translate_to_zh('Hello', session=session)
        self.assertEqual(ctx.exception.status_code, 429)

    def test_translate_email_fields_separates_subject_and_body(self):
        session = MagicMock()
        responses = []
        for translated in ('主题译', '正文译'):
            response = MagicMock()
            response.status_code = 200
            response.json.return_value = {
                'responseStatus': 200,
                'responseData': {'translatedText': translated},
            }
            responses.append(response)
        session.get.side_effect = responses

        result = translate_email_fields_to_zh(
            subject='Subject',
            body='Body',
            source_lang='en',
            session=session,
        )
        self.assertEqual(result['subject_translation'], '主题译')
        self.assertEqual(result['body_translation'], '正文译')
        self.assertEqual(result['translation'], '主题译\n\n正文译')


class TranslateApiTests(unittest.TestCase):
    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

    def test_requires_login(self):
        response = self.client.post('/api/emails/translate', json={'text': 'Hello'})
        self.assertIn(response.status_code, {302, 401, 403})

    def test_missing_body_returns_400(self):
        with self.client.session_transaction() as session:
            session['logged_in'] = True
        response = self.client.post('/api/emails/translate', json={})
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertFalse(data.get('success'))

    @patch('outlook_web.translate_mymemory.requests.get')
    def test_success_with_mocked_mymemory(self, mock_get):
        with self.client.session_transaction() as session:
            session['logged_in'] = True

        def build_response(text):
            response = MagicMock()
            response.status_code = 200
            response.json.return_value = {
                'responseStatus': 200,
                'responseData': {'translatedText': text},
            }
            return response

        mock_get.side_effect = [build_response('你好主题'), build_response('你好正文')]

        api_response = self.client.post('/api/emails/translate', json={
            'subject': 'Hi',
            'text': 'Hello',
            'source_lang': 'en',
        })
        self.assertEqual(api_response.status_code, 200)
        data = api_response.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['provider'], 'mymemory')
        self.assertEqual(data['subject_translation'], '你好主题')
        self.assertEqual(data['body_translation'], '你好正文')
        self.assertEqual(data['translation'], '你好主题\n\n你好正文')


if __name__ == '__main__':
    unittest.main()
