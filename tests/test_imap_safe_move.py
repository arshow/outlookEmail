"""Safe IMAP auto-move must never fall back to broad deletion semantics."""

import importlib
import os
import tempfile

import pytest


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
os.environ.setdefault(
    'DATABASE_PATH',
    os.path.join(tempfile.mkdtemp(prefix='imap-safe-move-'), 'test.db'),
)
mail_app = importlib.import_module('web_outlook_app')


class MoveMail:
    def __init__(self, *, source_exists=True, move_status='OK', move_data=None,
                 remove_source=False):
        self.source_exists = source_exists
        self.move_status = move_status
        self.move_data = move_data if move_data is not None else []
        self.remove_source = remove_source
        self.calls = []
        self.untagged_responses = {
            'UIDVALIDITY': [b'77'],
            'COPYUID': [b'COPYUID 8 1 2'],
        }

    def uid(self, command, *args):
        self.calls.append((command, args))
        if command == 'FETCH':
            if self.source_exists:
                return 'OK', [b'1 (UID 42)']
            return 'OK', [None]
        if command == 'MOVE':
            if self.remove_source:
                self.source_exists = False
            return self.move_status, self.move_data
        return 'BAD', [b'unexpected command']

    def response(self, code):
        return code, self.untagged_responses.pop(code, None)


@pytest.fixture
def resolved_imap_folders(monkeypatch):
    monkeypatch.setattr(
        mail_app,
        'resolve_imap_folder',
        lambda _mail, _provider, folder, readonly=True: (
            ('INBOX' if str(folder).lower() == 'inbox' else 'Junk', {})
        ),
    )


def move_one(mail):
    return mail_app.move_email_items_imap(
        mail,
        [{'id': 'imap:77:42', 'folder': 'junkemail', 'id_mode': 'uid'}],
        'custom',
        target_folder='inbox',
        safe_move=True,
    )


def test_safe_move_maps_only_valid_copyuid_and_discards_stale_response(resolved_imap_folders):
    mail = MoveMail(
        move_data=[b'A1 OK [COPYUID 88 42 900] MOVE completed'],
        remove_source=True,
    )

    result = move_one(mail)

    assert result['success'] is True
    assert result['moved_ids'] == ['imap:77:42']
    assert result['moved_id_map'] == {'imap:77:42': 'imap:88:900'}
    assert [call[0] for call in mail.calls] == ['FETCH', 'MOVE']
    assert 'COPYUID' not in mail.untagged_responses


@pytest.mark.parametrize('copyuid', [
    b'A1 OK [COPYUID 88 43 900] MOVE completed',
    b'A1 OK [COPYUID malformed] MOVE completed',
    b'A1 OK [COPYUID 88 42:43 900:901] MOVE completed',
])
def test_safe_move_rejects_malformed_or_mismatched_copyuid_without_fabricating_id(
    resolved_imap_folders, copyuid
):
    mail = MoveMail(move_data=[copyuid], remove_source=True)

    result = move_one(mail)

    assert result['success'] is False
    assert result['moved_id_map'] == {}
    assert any(error['id'] == 'imap:77:42' for error in result['errors'])
    assert [call[0] for call in mail.calls] == ['FETCH', 'MOVE']


def test_safe_move_refuses_tagged_ok_noop_and_never_uses_copy_or_expunge(
    resolved_imap_folders,
):
    mail = MoveMail(move_data=[b'A1 OK MOVE completed'], remove_source=False)

    result = move_one(mail)

    assert result['success'] is False
    assert result['moved_id_map'] == {}
    assert [call[0] for call in mail.calls] == ['FETCH', 'MOVE', 'FETCH']


def test_safe_move_confirms_source_removal_when_server_omits_copyuid(
    resolved_imap_folders,
):
    mail = MoveMail(move_data=[b'A1 OK MOVE completed'], remove_source=True)

    result = move_one(mail)

    assert result['success'] is True
    assert result['moved_ids'] == ['imap:77:42']
    assert result['moved_id_map'] == {}
    assert [call[0] for call in mail.calls] == ['FETCH', 'MOVE', 'FETCH']


def test_safe_move_does_not_attempt_move_when_source_uid_is_missing(
    resolved_imap_folders,
):
    mail = MoveMail(source_exists=False)

    result = move_one(mail)

    assert result['success'] is False
    assert [call[0] for call in mail.calls] == ['FETCH']


def test_safe_move_refuses_unsupported_move_without_legacy_fallback(
    resolved_imap_folders,
):
    mail = MoveMail(move_status='BAD', move_data=[b'UID MOVE not supported'])

    result = move_one(mail)

    assert result['success'] is False
    assert [call[0] for call in mail.calls] == ['FETCH', 'MOVE']


def test_server_specific_wrappers_forward_safe_move(monkeypatch):
    received = []

    def move_items(*_args, **kwargs):
        received.append(kwargs.get('safe_move'))
        return {'success': True}

    monkeypatch.setattr(mail_app, 'move_email_items_imap', move_items)
    monkeypatch.setattr(mail_app, 'get_access_token_imap', lambda *_args: 'access-token')

    class SocketContext:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class OAuthMail:
        def authenticate(self, *_args):
            return 'OK', []

        def logout(self):
            return 'BYE', []

    monkeypatch.setattr(mail_app, 'proxy_socket_context', lambda *_args: SocketContext())
    monkeypatch.setattr(mail_app.imaplib, 'IMAP4_SSL', lambda *_args, **_kwargs: OAuthMail())
    batch_result = mail_app.move_emails_imap_batch(
        'user@example.com', 'client', 'refresh', [], safe_move=True
    )

    class GenericMail:
        def login(self, *_args):
            return 'OK', []

        def logout(self):
            return 'BYE', []

    monkeypatch.setattr(mail_app, 'create_imap_connection', lambda *_args: GenericMail())
    monkeypatch.setattr(mail_app, 'send_imap_id', lambda *_args: None)
    generic_result = mail_app.move_emails_imap_generic_result(
        'user@example.com', 'password', 'imap.example.com', [], safe_move=True
    )

    assert batch_result == {'success': True}
    assert generic_result == {'success': True}
    assert received == [True, True]
