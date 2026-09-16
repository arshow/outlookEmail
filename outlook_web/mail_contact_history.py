"""Local contact-history queries for compose related-mail lists.

Phase 1 groups by customer email only (`thread_strategy=contact_email`).
Phase 2 can switch to subject+contact clustering without conversationId by
using `subject_normalized` already returned on each item.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence

from outlook_web.mail_reply_address import extract_first_email_address

CONTACT_HISTORY_DEFAULT_LIMIT = 20
CONTACT_HISTORY_MAX_LIMIT = 50
THREAD_STRATEGY_CONTACT_EMAIL = 'contact_email'
# Reserved for the next phase: group by stripped subject + contact email.
THREAD_STRATEGY_SUBJECT_CONTACT = 'subject_contact'

SUBJECT_THREAD_PREFIX_RE = re.compile(
    r'^(re|fw|fwd|回复|转发)\s*[:：]\s*',
    re.I,
)


def normalize_subject_for_thread(subject: Any) -> str:
    text = str(subject or '').strip()
    previous = None
    while text and text != previous:
        previous = text
        text = SUBJECT_THREAD_PREFIX_RE.sub('', text, count=1).strip()
    return text or '无主题'


def build_contact_thread_key(contact_email: Any) -> str:
    address = extract_first_email_address(contact_email)
    return f'contact:{address}' if address else ''


def clamp_contact_history_limit(
    value: Any,
    default: int = CONTACT_HISTORY_DEFAULT_LIMIT,
    max_limit: int = CONTACT_HISTORY_MAX_LIMIT,
) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = default
    ceiling = max(1, int(max_limit or CONTACT_HISTORY_MAX_LIMIT))
    if limit < 1:
        return default
    return min(limit, ceiling)


def clamp_contact_history_offset(value: Any) -> int:
    try:
        offset = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, offset)


def classify_contact_direction(
    *,
    sender: Any,
    recipients: Any = '',
    account_email: str = '',
    contact_email: str = '',
    body_text: str = '',
) -> str:
    sender_addr = extract_first_email_address(sender)
    account = extract_first_email_address(account_email)
    contact = extract_first_email_address(contact_email)
    if sender_addr and account and sender_addr == account:
        return 'outbound'
    if sender_addr and contact and sender_addr == contact:
        return 'inbound'
    haystack = f'{sender or ""}\n{recipients or ""}\n{body_text or ""}'.lower()
    if contact and contact in haystack and sender_addr != account:
        return 'inbound'
    return 'unknown'


def _contact_match_sql() -> str:
    return '''
        account_id = ?
        AND (
            instr(lower(coalesce(sender, '')), ?) > 0
            OR instr(lower(coalesce(recipients, '')), ?) > 0
            OR instr(lower(coalesce(cc, '')), ?) > 0
            OR instr(lower(coalesce(body_preview, '')), ?) > 0
            OR instr(lower(coalesce(body, '')), ?) > 0
        )
    '''


def _row_value(row: Any, key: str, default: Any = '') -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def format_contact_history_item(
    row: Any,
    *,
    account_email: str,
    contact_email: str,
    current_message_id: str = '',
    include_body: bool = False,
) -> Dict[str, Any]:
    message_id = str(_row_value(row, 'provider_message_id') or '')
    sender = str(_row_value(row, 'sender') or '')
    recipients = str(_row_value(row, 'recipients') or '')
    body_preview = str(_row_value(row, 'body_preview') or '')
    body = str(_row_value(row, 'body') or '')
    body_cached = bool(_row_value(row, 'body_cached') or 0)
    subject = str(_row_value(row, 'subject') or '无主题')
    match_text = body if body_cached and body else body_preview
    item = {
        'id': message_id,
        'subject': subject or '无主题',
        'subject_normalized': normalize_subject_for_thread(subject),
        'from': sender or '未知',
        'to': recipients,
        'cc': str(_row_value(row, 'cc') or ''),
        'date': str(_row_value(row, 'received_at') or ''),
        'folder': str(_row_value(row, 'folder') or 'inbox'),
        'id_mode': str(_row_value(row, 'id_mode') or ''),
        'direction': classify_contact_direction(
            sender=sender,
            recipients=recipients,
            account_email=account_email,
            contact_email=contact_email,
            body_text=match_text,
        ),
        'body_preview': body_preview[:240],
        'body_cached': body_cached,
        'is_current': bool(current_message_id and message_id == current_message_id),
        'thread_key': build_contact_thread_key(contact_email),
    }
    if include_body:
        item['body'] = body if body_cached else ''
        item['body_type'] = str(_row_value(row, 'body_type') or 'text')
        item['body_cached'] = body_cached
    return item


def fetch_local_contact_history(
    db,
    *,
    account_id: int,
    account_email: str,
    contact_email: str,
    current_message_id: str = '',
    limit: Any = CONTACT_HISTORY_DEFAULT_LIMIT,
    offset: Any = 0,
    include_current: bool = True,
    include_body: bool = False,
    max_limit: int = CONTACT_HISTORY_MAX_LIMIT,
) -> Dict[str, Any]:
    contact = extract_first_email_address(contact_email)
    thread_key = build_contact_thread_key(contact)
    payload = {
        'contact_email': contact,
        'thread_key': thread_key,
        'thread_strategy': THREAD_STRATEGY_CONTACT_EMAIL,
        'emails': [],
        'total': 0,
        'has_more': False,
    }
    account_id = int(account_id or 0)
    if not db or not account_id or not contact:
        return payload

    needle = contact.lower()
    match_sql = _contact_match_sql()
    match_params: Sequence[Any] = (account_id, needle, needle, needle, needle, needle)
    count_row = db.execute(
        f'SELECT COUNT(*) AS count FROM retained_normal_mail_messages WHERE {match_sql}',
        match_params,
    ).fetchone()
    if isinstance(count_row, dict):
        total = int(count_row.get('count') or 0)
    elif count_row is None:
        total = 0
    else:
        try:
            total = int(count_row['count'])
        except (KeyError, IndexError, TypeError):
            total = int(count_row[0] or 0)

    page_limit = clamp_contact_history_limit(limit, max_limit=max_limit)
    page_offset = clamp_contact_history_offset(offset)
    current_id = str(current_message_id or '').strip()
    rows = db.execute(
        f'''
        SELECT provider_message_id, id_mode, folder, subject, sender, recipients, cc,
               received_at, body_preview, body, body_type, body_cached
        FROM retained_normal_mail_messages
        WHERE {match_sql}
        ORDER BY received_at_sort DESC, id DESC
        LIMIT ? OFFSET ?
        ''',
        tuple(match_params) + (page_limit, page_offset),
    ).fetchall()

    emails: List[Dict[str, Any]] = []
    for row in rows:
        item = format_contact_history_item(
            row,
            account_email=account_email,
            contact_email=contact,
            current_message_id=current_id,
            include_body=include_body,
        )
        if not include_current and item.get('is_current'):
            continue
        emails.append(item)

    payload['emails'] = emails
    payload['total'] = total
    payload['has_more'] = (page_offset + len(rows)) < total
    return payload
