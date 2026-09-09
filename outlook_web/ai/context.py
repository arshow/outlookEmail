"""Build email context for AI analysis."""

from __future__ import annotations

import re
from html import unescape
from typing import Any, Dict, List, Tuple

from outlook_web.ai.constants import (
    CONTEXT_SCOPE_CONTACT_LOCAL,
    CONTEXT_SCOPE_CURRENT,
    HISTORY_BODY_MAX_CHARS,
    HISTORY_MAX_MESSAGES,
)

EMAIL_RE = re.compile(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}', re.I)
TAG_RE = re.compile(r'<[^>]+>')
DATA_IMAGE_RE = re.compile(r'(?is)data:image/[a-z0-9.+-]+;base64,[a-z0-9+/=\s]+')
IMG_TAG_RE = re.compile(r'(?is)<img\b[^>]*>')
BASE64_BLOB_RE = re.compile(r'(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{400,}={0,2}')


def strip_embedded_media(value: Any) -> str:
    """Drop inline images / base64 blobs so AI sees the real email text."""
    text = str(value or '')
    if not text:
        return ''
    text = DATA_IMAGE_RE.sub('[图片]', text)
    text = IMG_TAG_RE.sub('[图片]', text)
    text = BASE64_BLOB_RE.sub('[附件内容已省略]', text)
    return text


def format_attachment_note(attachments: Any) -> str:
    names: List[str] = []
    if isinstance(attachments, list):
        for item in attachments:
            if isinstance(item, dict):
                name = str(item.get('name') or item.get('filename') or '').strip()
            else:
                name = str(item or '').strip()
            if name and name not in names:
                names.append(name)
            if len(names) >= 10:
                break
    if not names:
        return ''
    return f'[附件: {", ".join(names)}]'


def extract_email_address(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, dict):
        for key in ('address', 'email', 'emailAddress'):
            nested = value.get(key)
            if isinstance(nested, dict):
                candidate = nested.get('address') or nested.get('email') or ''
                if candidate:
                    return str(candidate).strip().lower()
            if nested:
                text = str(nested)
                match = EMAIL_RE.search(text)
                if match:
                    return match.group(0).lower()
        text = str(value.get('name') or value)
    else:
        text = str(value)
    match = EMAIL_RE.search(text)
    return match.group(0).lower() if match else ''


def html_to_text(value: Any) -> str:
    text = strip_embedded_media(value)
    text = re.sub(r'(?i)<br\s*/?>', '\n', text)
    text = re.sub(r'(?i)</p>', '\n', text)
    text = TAG_RE.sub('', text)
    text = unescape(text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def truncate_text(value: str, limit: int = HISTORY_BODY_MAX_CHARS) -> str:
    text = str(value or '').strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + '…'


def normalize_email_detail(detail: Dict[str, Any]) -> Dict[str, Any]:
    body = detail.get('body') or detail.get('bodyPreview') or detail.get('body_preview') or ''
    body_type = str(detail.get('body_type') or detail.get('bodyType') or 'text').lower()
    if isinstance(body, dict):
        body_type = str(body.get('contentType') or body_type).lower()
        body = body.get('content') or ''
    body_text = html_to_text(body) if 'html' in body_type else strip_embedded_media(body)
    attachment_note = format_attachment_note(detail.get('attachments'))
    if attachment_note:
        body_text = f'{body_text}\n\n{attachment_note}'.strip()
    sender = extract_email_address(detail.get('from') or detail.get('sender'))
    return {
        'id': str(detail.get('id') or detail.get('provider_message_id') or ''),
        'subject': str(detail.get('subject') or '无主题'),
        'from': sender or str(detail.get('from') or detail.get('sender') or ''),
        'to': detail.get('to') or detail.get('toRecipients') or detail.get('recipients') or '',
        'received_at': str(detail.get('receivedDateTime') or detail.get('received_at') or detail.get('date') or ''),
        'body_text': truncate_text(body_text, HISTORY_BODY_MAX_CHARS * 2),
        'body_preview': truncate_text(str(detail.get('bodyPreview') or detail.get('body_preview') or body_text), 500),
    }


def resolve_contact_email(current: Dict[str, Any], account_email: str) -> str:
    account = str(account_email or '').strip().lower()
    sender = extract_email_address(current.get('from'))
    if sender and sender != account:
        return sender
    # Fall back to first external recipient (rare for inbound).
    recipients = current.get('to')
    if isinstance(recipients, list):
        for item in recipients:
            address = extract_email_address(item)
            if address and address != account:
                return address
    if isinstance(recipients, str):
        for match in EMAIL_RE.findall(recipients):
            address = match.lower()
            if address != account:
                return address
    return sender


def load_contact_local_history(
    db,
    *,
    account_id: int,
    account_email: str,
    contact_email: str,
    exclude_message_id: str = '',
    limit: int = HISTORY_MAX_MESSAGES,
) -> List[Dict[str, Any]]:
    contact = str(contact_email or '').strip().lower()
    if not contact or not account_id:
        return []
    like = f'%{contact}%'
    rows = db.execute(
        '''
        SELECT provider_message_id, subject, sender, recipients, received_at, body, body_type, body_preview, body_cached
        FROM retained_normal_mail_messages
        WHERE account_id = ?
          AND (
            LOWER(COALESCE(sender, '')) LIKE ?
            OR LOWER(COALESCE(recipients, '')) LIKE ?
          )
        ORDER BY received_at_sort DESC, id DESC
        LIMIT ?
        ''',
        (account_id, like, like, max(1, min(int(limit or HISTORY_MAX_MESSAGES), HISTORY_MAX_MESSAGES))),
    ).fetchall()

    account = str(account_email or '').strip().lower()
    exclude = str(exclude_message_id or '').strip()
    messages: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        message_id = str(item.get('provider_message_id') or '')
        if exclude and message_id == exclude:
            continue
        sender = extract_email_address(item.get('sender'))
        body = item.get('body') if item.get('body_cached') else (item.get('body_preview') or '')
        body_type = str(item.get('body_type') or 'text').lower()
        body_text = html_to_text(body) if 'html' in body_type else str(body or '')
        direction = 'inbound' if sender == contact else ('outbound' if sender == account else 'unknown')
        messages.append({
            'id': message_id,
            'subject': str(item.get('subject') or '无主题'),
            'from': sender or str(item.get('sender') or ''),
            'received_at': str(item.get('received_at') or ''),
            'direction': direction,
            'body_text': truncate_text(body_text),
        })
    # Chronological order for the prompt.
    messages.reverse()
    return messages


def build_analysis_context(
    *,
    scope: str,
    current_detail: Dict[str, Any],
    account_email: str,
    account_id: int,
    db=None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (context_for_prompt, meta_for_response)."""
    current = normalize_email_detail(current_detail)
    contact_email = resolve_contact_email(current, account_email)
    selected_scope = CONTEXT_SCOPE_CURRENT
    history: List[Dict[str, Any]] = []
    degraded = False
    degrade_reason = ''

    requested = str(scope or CONTEXT_SCOPE_CURRENT).strip().lower()
    if requested == CONTEXT_SCOPE_CONTACT_LOCAL:
        if db is None or not account_id or not contact_email:
            degraded = True
            degrade_reason = '本地无该联系人历史或无法解析对方地址'
        else:
            history = load_contact_local_history(
                db,
                account_id=account_id,
                account_email=account_email,
                contact_email=contact_email,
                exclude_message_id=current.get('id') or '',
            )
            if history:
                selected_scope = CONTEXT_SCOPE_CONTACT_LOCAL
            else:
                degraded = True
                degrade_reason = '本地无该联系人历史'

    context = {
        'scope': selected_scope,
        'accountEmail': account_email,
        'contactEmail': contact_email,
        'currentEmail': current,
        'historyMessages': history,
        'historyCount': len(history),
    }
    meta = {
        'context_scope': selected_scope,
        'requested_scope': requested,
        'contact_email': contact_email,
        'history_count': len(history),
        'degraded': degraded,
        'degrade_reason': degrade_reason,
    }
    return context, meta


def context_haystack(context: Dict[str, Any]) -> str:
    parts = [
        str((context.get('currentEmail') or {}).get('subject') or ''),
        str((context.get('currentEmail') or {}).get('body_text') or ''),
    ]
    for message in context.get('historyMessages') or []:
        parts.append(str(message.get('subject') or ''))
        parts.append(str(message.get('body_text') or ''))
    return '\n'.join(parts)
