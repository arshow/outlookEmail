import importlib
import os
import tempfile
import unittest
from unittest.mock import patch


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
os.environ.setdefault('DATABASE_PATH', os.path.join(tempfile.mkdtemp(prefix='imap-search-'), 'test.db'))
web_outlook_app = importlib.import_module('web_outlook_app')


class SearchMail:
    def __init__(self, uid_error=None, search_error=None):
        self.uid_error = uid_error
        self.search_error = search_error
        self.logged_out = False

    def uid(self, command, *args):
        if self.uid_error:
            raise self.uid_error
        return 'BAD', [b'rejected']

    def search(self, *args):
        if self.search_error:
            raise self.search_error
        return 'BAD', [b'rejected']

    def logout(self):
        self.logged_out = True


class ImapSearchFallbackTests(unittest.TestCase):
    def test_protocol_rejection_uses_sequence_numbers(self):
        mail = SearchMail()
        message_ids, mode, attempts = web_outlook_app.search_imap_message_ids(mail)
        self.assertIsNone(message_ids)
        resolved, ids, resolved_mode = web_outlook_app.finalize_imap_search_ids(
            mail, message_ids, mode, attempts, 3, lambda: (None, 0)
        )
        self.assertIs(resolved, mail)
        self.assertEqual(resolved_mode, 'sequence')
        self.assertEqual(ids, [b'1', b'2', b'3'])

    def test_oversized_search_reconnects_before_sequence_enumeration(self):
        mail = SearchMail(uid_error=Exception('got more than 1000000 bytes'),
                          search_error=Exception('got more than 1000000 bytes'))
        replacement = SearchMail()
        message_ids, mode, attempts = web_outlook_app.search_imap_message_ids(mail)
        resolved, ids, resolved_mode = web_outlook_app.finalize_imap_search_ids(
            mail, message_ids, mode, attempts, 2, lambda: (replacement, 2)
        )
        self.assertIs(resolved, replacement)
        self.assertTrue(mail.logged_out)
        self.assertEqual(resolved_mode, 'sequence')
        self.assertEqual(ids, [b'1', b'2'])

    def test_gmail_login_retries_once_then_raises(self):
        class Broken:
            def __init__(self):
                self.sock = None
                self.attempts = 0

            def login(self, *args):
                self.attempts += 1
                raise TimeoutError('timed out')

            def shutdown(self):
                pass

        created = []

        def connect(*args, **kwargs):
            mail = Broken()
            created.append(mail)
            return mail

        with patch.object(web_outlook_app, 'create_imap_connection', side_effect=connect):
            result = web_outlook_app.get_emails_imap_generic(
                'user@gmail.com', 'secret', 'imap.gmail.com', folder='inbox'
            )

        self.assertFalse(result['success'])
        self.assertEqual(len(created), 2)
        self.assertEqual(created[0].attempts, 1)


if __name__ == '__main__':
    unittest.main()
