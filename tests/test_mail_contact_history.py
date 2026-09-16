import sqlite3
import unittest

from outlook_web.mail_contact_history import (
    THREAD_STRATEGY_CONTACT_EMAIL,
    build_contact_thread_key,
    classify_contact_direction,
    fetch_local_contact_history,
    normalize_subject_for_thread,
)

SHOPIFY_BODY = '''
<div class="primary-message">You received a new message from your online store's contact form.</div>
<div class="form-section"><b>Email:</b> <pre>luca.giust@libero.it</pre></div>
'''


def _make_db():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute(
        '''
        CREATE TABLE retained_normal_mail_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            folder TEXT NOT NULL DEFAULT 'inbox',
            provider_message_id TEXT NOT NULL,
            id_mode TEXT NOT NULL DEFAULT '',
            subject TEXT DEFAULT '无主题',
            sender TEXT DEFAULT '未知',
            recipients TEXT DEFAULT '',
            cc TEXT DEFAULT '',
            received_at TEXT DEFAULT '',
            received_at_sort REAL DEFAULT 0,
            body_preview TEXT DEFAULT '',
            body TEXT,
            body_type TEXT DEFAULT 'text',
            body_cached INTEGER NOT NULL DEFAULT 0
        )
        '''
    )
    return db


def _insert(db, **kwargs):
    defaults = {
        'account_id': 1,
        'folder': 'inbox',
        'provider_message_id': 'msg-1',
        'id_mode': '',
        'subject': 'Hello',
        'sender': 'customer@example.com',
        'recipients': 'store@example.com',
        'cc': '',
        'received_at': '2026-01-01 10:00:00',
        'received_at_sort': 1,
        'body_preview': '',
        'body': '',
        'body_type': 'html',
        'body_cached': 0,
    }
    defaults.update(kwargs)
    db.execute(
        '''
        INSERT INTO retained_normal_mail_messages (
            account_id, folder, provider_message_id, id_mode, subject, sender,
            recipients, cc, received_at, received_at_sort, body_preview, body, body_type, body_cached
        ) VALUES (
            :account_id, :folder, :provider_message_id, :id_mode, :subject, :sender,
            :recipients, :cc, :received_at, :received_at_sort, :body_preview, :body, :body_type, :body_cached
        )
        ''',
        defaults,
    )


class MailContactHistoryTests(unittest.TestCase):
    def test_thread_key_and_subject_normalization(self):
        self.assertEqual(build_contact_thread_key('Luca <luca.giust@libero.it>'), 'contact:luca.giust@libero.it')
        self.assertEqual(normalize_subject_for_thread('Re: Fw: 回复: Need update'), 'Need update')
        self.assertEqual(classify_contact_direction(
            sender='mailer@shopify.com',
            body_text=SHOPIFY_BODY,
            account_email='store@example.com',
            contact_email='luca.giust@libero.it',
        ), 'inbound')

    def test_matches_shopify_contact_form_body_and_regular_sender(self):
        db = _make_db()
        _insert(
            db,
            provider_message_id='shopify-1',
            subject='New customer message on September 15, 2026 at 10:45 am',
            sender='mailer@shopify.com',
            recipients='store@example.com',
            body=SHOPIFY_BODY,
            body_cached=1,
            received_at_sort=3,
        )
        _insert(
            db,
            provider_message_id='reply-1',
            folder='sentitems',
            subject='Re: New customer message on September 15, 2026 at 10:45 am',
            sender='store@example.com',
            recipients='luca.giust@libero.it',
            body_preview='Thanks for your message',
            received_at_sort=2,
        )
        _insert(
            db,
            provider_message_id='other-1',
            subject='Hello',
            sender='someone@example.com',
            recipients='store@example.com',
            body='unrelated',
            body_cached=1,
            received_at_sort=1,
        )
        result = fetch_local_contact_history(
            db,
            account_id=1,
            account_email='store@example.com',
            contact_email='luca.giust@libero.it',
            current_message_id='shopify-1',
        )
        ids = [item['id'] for item in result['emails']]
        self.assertEqual(ids, ['shopify-1', 'reply-1'])
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['thread_strategy'], THREAD_STRATEGY_CONTACT_EMAIL)
        self.assertTrue(result['emails'][0]['is_current'])
        self.assertEqual(result['emails'][0]['direction'], 'inbound')
        self.assertEqual(result['emails'][1]['direction'], 'outbound')
        self.assertEqual(result['emails'][0]['subject_normalized'], 'New customer message on September 15, 2026 at 10:45 am')
        self.assertEqual(result['emails'][1]['subject_normalized'], 'New customer message on September 15, 2026 at 10:45 am')

    def test_pagination_and_missing_contact(self):
        db = _make_db()
        for index in range(3):
            _insert(
                db,
                provider_message_id=f'msg-{index}',
                sender='customer@example.com',
                received_at_sort=index,
            )
        page = fetch_local_contact_history(
            db,
            account_id=1,
            account_email='store@example.com',
            contact_email='customer@example.com',
            limit=2,
            offset=0,
        )
        self.assertEqual(page['total'], 3)
        self.assertTrue(page['has_more'])
        self.assertEqual(len(page['emails']), 2)
        empty = fetch_local_contact_history(
            db,
            account_id=1,
            account_email='store@example.com',
            contact_email='',
        )
        self.assertEqual(empty['emails'], [])
        self.assertEqual(empty['thread_key'], '')


if __name__ == '__main__':
    unittest.main()
