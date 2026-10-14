"""Mark email-list items that already have an AI reply draft."""

from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence, Set, Tuple

AI_DRAFT_LOOKUP_CHUNK_SIZE = 200
AIDraftKey = Tuple[str, str]


def normalize_ai_draft_account_email(value: Any) -> str:
    return str(value or '').strip().lower()


def normalize_ai_draft_message_id(value: Any) -> str:
    return str(value or '').strip()


def ai_draft_lookup_key(account_email: Any, message_id: Any) -> AIDraftKey:
    return (
        normalize_ai_draft_account_email(account_email),
        normalize_ai_draft_message_id(message_id),
    )


def fetch_ai_draft_message_keys(db, keys: Sequence[AIDraftKey]) -> Set[AIDraftKey]:
    unique_keys: List[AIDraftKey] = []
    seen: Set[AIDraftKey] = set()
    for key in keys or []:
        if not isinstance(key, tuple) or len(key) != 2:
            continue
        normalized = ai_draft_lookup_key(*key)
        if not normalized[0] or not normalized[1] or normalized in seen:
            continue
        seen.add(normalized)
        unique_keys.append(normalized)
    if db is None or not unique_keys:
        return set()

    found: Set[AIDraftKey] = set()
    for index in range(0, len(unique_keys), AI_DRAFT_LOOKUP_CHUNK_SIZE):
        chunk = unique_keys[index:index + AI_DRAFT_LOOKUP_CHUNK_SIZE]
        emails = sorted({item[0] for item in chunk})
        message_ids = sorted({item[1] for item in chunk})
        email_placeholders = ','.join('?' * len(emails))
        message_placeholders = ','.join('?' * len(message_ids))
        rows = db.execute(
            f'''
            SELECT r.account_email, r.message_id
            FROM ai_analysis_runs r
            INNER JOIN ai_drafts d ON d.run_id = r.id
            WHERE r.status = 'succeeded'
              AND TRIM(IFNULL(d.reply_text, '')) != ''
              AND lower(r.account_email) IN ({email_placeholders})
              AND r.message_id IN ({message_placeholders})
            ''',
            [*emails, *message_ids],
        ).fetchall()
        wanted = set(chunk)
        for row in rows:
            key = ai_draft_lookup_key(row['account_email'], row['message_id'])
            if key in wanted:
                found.add(key)
    return found


def attach_ai_draft_flags_to_email_items(
    db,
    emails: Optional[Iterable[Any]],
    *,
    default_account_email: Any = '',
) -> List[Any]:
    items = list(emails or [])
    keys: List[AIDraftKey] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        keys.append(ai_draft_lookup_key(
            item.get('account_email') or default_account_email,
            item.get('id') or item.get('message_id'),
        ))
    found = fetch_ai_draft_message_keys(db, keys)
    for item in items:
        if not isinstance(item, dict):
            continue
        key = ai_draft_lookup_key(
            item.get('account_email') or default_account_email,
            item.get('id') or item.get('message_id'),
        )
        item['has_ai_draft'] = key in found
    return items
