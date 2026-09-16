"""Per-email notes stored locally, independent of mail retention."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

EMAIL_NOTE_MAX_LENGTH = 2000
EMAIL_NOTE_MESSAGE_ID_MAX_LENGTH = 2048
EMAIL_NOTE_ID_MODE_MAX_LENGTH = 32
EMAIL_NOTE_LOOKUP_KEY = Tuple[int, str, str, str]


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


def attach_notes_to_email_items(
    db,
    emails: Optional[Iterable[Any]],
    *,
    default_account_id: Any = 0,
    default_folder: Any = 'inbox',
) -> List[Any]:
    items = list(emails or [])
    keys = []
    for item in items:
        if not isinstance(item, dict):
            continue
        keys.append(email_note_lookup_key(
            item.get('account_id') or default_account_id,
            item.get('folder') or default_folder,
            item.get('id'),
            item.get('id_mode'),
        ))
    notes = fetch_email_notes_map(db, keys)
    for item in items:
        if not isinstance(item, dict):
            continue
        key = email_note_lookup_key(
            item.get('account_id') or default_account_id,
            item.get('folder') or default_folder,
            item.get('id'),
            item.get('id_mode'),
        )
        item['note'] = notes.get(key, '')
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
