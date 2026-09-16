"""Per-email notes stored locally, independent of mail retention."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from outlook_web.mail_reply_address import (
    attach_preferred_reply_address,
    extract_first_email_address,
    is_shopify_platform_address,
    resolve_preferred_reply_address,
)

EMAIL_NOTE_MAX_LENGTH = 2000
EMAIL_NOTE_MESSAGE_ID_MAX_LENGTH = 2048
EMAIL_NOTE_ID_MODE_MAX_LENGTH = 32
EMAIL_NOTE_LOOKUP_KEY = Tuple[int, str, str, str]
CONTACT_NOTE_KEY = Tuple[int, str]


def normalize_email_note_text(value: Any, max_length: int = EMAIL_NOTE_MAX_LENGTH) -> str:
    text = str(value or '')
    text = ''.join(ch for ch in text if ch.isprintable() or ch in '\n\t')
    ceiling = max(1, int(max_length or EMAIL_NOTE_MAX_LENGTH))
    return text.strip()[:ceiling]


def normalize_email_note_message_id(value: Any) -> str:
    return str(value or '').strip()[:EMAIL_NOTE_MESSAGE_ID_MAX_LENGTH]


def normalize_email_note_id_mode(value: Any) -> str:
    return str(value or '').strip().lower()[:EMAIL_NOTE_ID_MODE_MAX_LENGTH]


def normalize_email_note_folder(value: Any) -> str:
    folder = str(value or 'inbox').strip() or 'inbox'
    return folder[:256]


def email_note_lookup_key(
    account_id: Any,
    folder: Any,
    message_id: Any,
    id_mode: Any,
) -> EMAIL_NOTE_LOOKUP_KEY:
    try:
        normalized_account_id = int(account_id or 0)
    except (TypeError, ValueError):
        normalized_account_id = 0
    return (
        normalized_account_id,
        normalize_email_note_folder(folder),
        normalize_email_note_message_id(message_id),
        normalize_email_note_id_mode(id_mode),
    )


def resolve_contact_email_for_item(item: Any, account_email: Any = '') -> str:
    detail = item if isinstance(item, dict) else {}
    attach_preferred_reply_address(detail)
    account = extract_first_email_address(account_email or detail.get('account_email'))
    preferred = resolve_preferred_reply_address(
        sender=detail.get('from') or detail.get('sender') or '',
        subject=detail.get('subject') or '',
        body=detail.get('body') or detail.get('body_preview') or '',
        header_reply_to=detail.get('reply_to') or '',
    )
    candidates = [
        preferred,
        extract_first_email_address(detail.get('reply_to')),
        extract_first_email_address(detail.get('from') or detail.get('sender')),
        extract_first_email_address(detail.get('to')),
    ]
    for address in candidates:
        if address and address != account and not is_shopify_platform_address(address):
            return address
    return ''


def contact_note_lookup_key(account_id: Any, contact_email: Any) -> CONTACT_NOTE_KEY:
    try:
        normalized_account_id = int(account_id or 0)
    except (TypeError, ValueError):
        normalized_account_id = 0
    return (normalized_account_id, extract_first_email_address(contact_email))


def fetch_email_notes_map(db, keys: Sequence[EMAIL_NOTE_LOOKUP_KEY]) -> Dict[EMAIL_NOTE_LOOKUP_KEY, str]:
    unique_keys: List[EMAIL_NOTE_LOOKUP_KEY] = []
    seen = set()
    for key in keys or []:
        if not isinstance(key, tuple) or len(key) != 4:
            continue
        account_id, folder, message_id, id_mode = email_note_lookup_key(*key)
        if account_id <= 0 or not message_id:
            continue
        normalized = (account_id, folder, message_id, id_mode)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique_keys.append(normalized)
    if db is None or not unique_keys:
        return {}

    account_ids = sorted({key[0] for key in unique_keys})
    message_ids = sorted({key[2] for key in unique_keys})
    placeholders_accounts = ','.join('?' * len(account_ids))
    placeholders_messages = ','.join('?' * len(message_ids))
    rows = db.execute(
        f'''
        SELECT account_id, folder, provider_message_id, id_mode, note
        FROM email_notes
        WHERE account_id IN ({placeholders_accounts})
          AND provider_message_id IN ({placeholders_messages})
        ''',
        [*account_ids, *message_ids],
    ).fetchall()

    wanted = set(unique_keys)
    notes: Dict[EMAIL_NOTE_LOOKUP_KEY, str] = {}
    for row in rows:
        key = email_note_lookup_key(
            row['account_id'],
            row['folder'],
            row['provider_message_id'],
            row['id_mode'],
        )
        if key in wanted:
            notes[key] = str(row['note'] or '')
    return notes


def fetch_contact_notes_map(db, keys: Sequence[CONTACT_NOTE_KEY]) -> Dict[CONTACT_NOTE_KEY, str]:
    unique_keys: List[CONTACT_NOTE_KEY] = []
    seen = set()
    for key in keys or []:
        if not isinstance(key, tuple) or len(key) != 2:
            continue
        normalized = contact_note_lookup_key(*key)
        if normalized[0] <= 0 or not normalized[1] or normalized in seen:
            continue
        seen.add(normalized)
        unique_keys.append(normalized)
    if db is None or not unique_keys:
        return {}

    account_ids = sorted({key[0] for key in unique_keys})
    contact_emails = sorted({key[1] for key in unique_keys})
    placeholders_accounts = ','.join('?' * len(account_ids))
    placeholders_contacts = ','.join('?' * len(contact_emails))
    rows = db.execute(
        f'''
        SELECT account_id, contact_email, note
        FROM contact_notes
        WHERE account_id IN ({placeholders_accounts})
          AND contact_email IN ({placeholders_contacts})
        ''',
        [*account_ids, *contact_emails],
    ).fetchall()

    wanted = set(unique_keys)
    notes: Dict[CONTACT_NOTE_KEY, str] = {}
    for row in rows:
        key = contact_note_lookup_key(row['account_id'], row['contact_email'])
        if key in wanted:
            notes[key] = str(row['note'] or '')
    return notes


def attach_notes_to_email_items(
    db,
    emails: Optional[Iterable[Any]],
    *,
    default_account_id: Any = 0,
    default_folder: Any = 'inbox',
    default_account_email: Any = '',
) -> List[Any]:
    items = list(emails or [])
    message_keys = []
    contact_keys = []
    resolved_contacts: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            resolved_contacts.append('')
            continue
        account_id = item.get('account_id') or default_account_id
        message_keys.append(email_note_lookup_key(
            account_id,
            item.get('folder') or default_folder,
            item.get('id'),
            item.get('id_mode'),
        ))
        contact = resolve_contact_email_for_item(
            item,
            item.get('account_email') or default_account_email,
        )
        resolved_contacts.append(contact)
        contact_keys.append(contact_note_lookup_key(account_id, contact))
    notes = fetch_email_notes_map(db, message_keys)
    try:
        contact_notes = fetch_contact_notes_map(db, contact_keys)
    except Exception:
        contact_notes = {}
    item_index = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        account_id = item.get('account_id') or default_account_id
        key = email_note_lookup_key(
            account_id,
            item.get('folder') or default_folder,
            item.get('id'),
            item.get('id_mode'),
        )
        contact = resolved_contacts[item_index] if item_index < len(resolved_contacts) else ''
        item_index += 1
        contact_note = contact_notes.get(contact_note_lookup_key(account_id, contact), '')
        per_message_note = notes.get(key, '')
        item['contact_email'] = contact
        item['contact_note'] = contact_note
        item['note'] = per_message_note or contact_note
    return items


def save_email_note(
    db,
    *,
    account_id: Any,
    folder: Any,
    message_id: Any,
    id_mode: Any = '',
    note: Any = '',
    commit: bool = True,
) -> str:
    key = email_note_lookup_key(account_id, folder, message_id, id_mode)
    normalized_note = normalize_email_note_text(note)
    if key[0] <= 0 or not key[2]:
        raise ValueError('invalid_email_note_key')

    if not normalized_note:
        db.execute(
            '''
            DELETE FROM email_notes
            WHERE account_id = ? AND folder = ? AND provider_message_id = ? AND id_mode = ?
            ''',
            key,
        )
    else:
        db.execute(
            '''
            INSERT INTO email_notes (
                account_id, folder, provider_message_id, id_mode, note, updated_at
            ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(account_id, folder, provider_message_id, id_mode)
            DO UPDATE SET
                note = excluded.note,
                updated_at = CURRENT_TIMESTAMP
            ''',
            (*key, normalized_note),
        )
    if commit:
        db.commit()
    return normalized_note


def save_contact_note(
    db,
    *,
    account_id: Any,
    contact_email: Any,
    note: Any = '',
    commit: bool = True,
) -> str:
    key = contact_note_lookup_key(account_id, contact_email)
    normalized_note = normalize_email_note_text(note)
    if key[0] <= 0 or not key[1]:
        raise ValueError('invalid_contact_note_key')

    if not normalized_note:
        db.execute(
            '''
            DELETE FROM contact_notes
            WHERE account_id = ? AND contact_email = ?
            ''',
            key,
        )
    else:
        db.execute(
            '''
            INSERT INTO contact_notes (
                account_id, contact_email, note, updated_at
            ) VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(account_id, contact_email)
            DO UPDATE SET
                note = excluded.note,
                updated_at = CURRENT_TIMESTAMP
            ''',
            (*key, normalized_note),
        )
    if commit:
        db.commit()
    return normalized_note
