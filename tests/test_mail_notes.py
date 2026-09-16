import sqlite3
import unittest

from outlook_web.mail_notes import (
    EMAIL_NOTE_MAX_LENGTH,
    attach_notes_to_email_items,
    email_note_lookup_key,
    normalize_email_note_text,
    resolve_contact_email_for_item,
    save_contact_note,
    save_email_note,
)


def _make_db():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute(
        '''
        CREATE TABLE email_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            folder TEXT NOT NULL DEFAULT 'inbox',
            provider_message_id TEXT NOT NULL,
            id_mode TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        '''
    )
    db.execute(
        '''
        CREATE UNIQUE INDEX ux_email_notes_key
        ON email_notes(account_id, folder, provider_message_id, id_mode)
        '''
    )
    db.execute(
        '''
        CREATE TABLE contact_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            contact_email TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        '''
    )
    db.execute(
        '''
        CREATE UNIQUE INDEX ux_contact_notes_key
        ON contact_notes(account_id, contact_email)
        '''
    )
    return db


class MailNotesHelperTests(unittest.TestCase):
    def test_normalize_strips_and_truncates(self):
        self.assertEqual(normalize_email_note_text('  hello\x00  '), 'hello')
        self.assertEqual(len(normalize_email_note_text('x' * 5000)), EMAIL_NOTE_MAX_LENGTH)
        self.assertEqual(email_note_lookup_key('3', 'Inbox', ' abc ', 'GRAPH'), (3, 'Inbox', 'abc', 'graph'))

    def test_save_and_attach_note(self):
        db = _make_db()
        saved = save_email_note(
            db,
            account_id=1,
            folder='inbox',
            message_id='msg-1',
            id_mode='graph',
            note='  客户要补发  ',
        )
        self.assertEqual(saved, '客户要补发')
        items = attach_notes_to_email_items(
            db,
            [
                {'id': 'msg-1', 'folder': 'inbox', 'id_mode': 'graph'},
                {'id': 'msg-2', 'folder': 'inbox', 'id_mode': 'graph'},
            ],
            default_account_id=1,
        )
        self.assertEqual(items[0]['note'], '客户要补发')
        self.assertEqual(items[1]['note'], '')

    def test_empty_note_deletes_row(self):
        db = _make_db()
        save_email_note(db, account_id=1, folder='inbox', message_id='msg-1', id_mode='', note='keep')
        save_email_note(db, account_id=1, folder='inbox', message_id='msg-1', id_mode='', note='  ')
        row = db.execute('SELECT COUNT(*) AS c FROM email_notes').fetchone()
        self.assertEqual(row['c'], 0)

    def test_contact_note_fills_other_emails_from_same_sender(self):
        db = _make_db()
        save_contact_note(db, account_id=1, contact_email='Buyer@Example.com', note='VIP 客户')
        items = attach_notes_to_email_items(
            db,
            [
                {'id': 'a', 'folder': 'inbox', 'id_mode': 'graph', 'from': 'buyer@example.com'},
                {'id': 'b', 'folder': 'inbox', 'id_mode': 'graph', 'from': 'buyer@example.com'},
                {'id': 'c', 'folder': 'inbox', 'id_mode': 'graph', 'from': 'other@example.com'},
            ],
            default_account_id=1,
            default_account_email='store@example.com',
        )
        self.assertEqual(items[0]['contact_email'], 'buyer@example.com')
        self.assertEqual(items[0]['note'], 'VIP 客户')
        self.assertEqual(items[1]['note'], 'VIP 客户')
        self.assertEqual(items[2]['note'], '')

    def test_shopify_contact_form_uses_customer_not_mailer(self):
        body = (
            '<div class="primary-message">You received a new message from your online store\'s contact form.</div>'
            '<div class="form-section"><b>Email:</b> <pre>luca.giust@libero.it</pre></div>'
        )
        contact = resolve_contact_email_for_item(
            {
                'from': 'mailer@shopify.com',
                'subject': 'New customer message on September 15, 2026',
                'body': body,
            },
            'store@example.com',
        )
        self.assertEqual(contact, 'luca.giust@libero.it')


if __name__ == '__main__':
    unittest.main()
