"""Resolve the real reply recipient for notification-style emails."""

from __future__ import annotations

import re
from typing import Any, Dict

EMAIL_RE = re.compile(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}', re.I)
SHOPIFY_PLATFORM_DOMAIN_RE = re.compile(
    r'(^|@)(?:[a-z0-9-]+\.)*(shopify\.com|shopifyemail\.com)$',
    re.I,
)
SHOPIFY_CONTACT_FORM_MARKERS = (
    "you received a new message from your online store's contact form",
    'from your online store&#39;s contact form',
    'from your online store&apos;s contact form',
    'from your online store&#x27;s contact form',
    'new customer message on',
    'class="form-section"',
    "class='form-section'",
    'class="mail-section mail-section--type-primary"',
)
SHOPIFY_FORM_EMAIL_PATTERNS = (
    re.compile(
        r'(?is)<b>\s*e-?mail(?:\s*address)?\s*:?\s*</b>\s*<pre[^>]*>\s*([^<]+?)\s*</pre>'
    ),
    re.compile(
        r'(?is)<b>\s*e-?mail(?:\s*address)?\s*:?\s*</b>\s*(?:<[^>]+>\s*)*'
        r'([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})'
    ),
    re.compile(
        r'(?im)^\s*e-?mail(?:\s*address)?\s*:\s*([^\s<>]+@[^\s<>]+)\s*$'
    ),
    re.compile(
        r'(?im)^\s*e-?mail(?:\s*address)?\s*:\s*[\r\n]+\s*([^\s<>]+@[^\s<>]+)\s*$'
    ),
)


def extract_first_email_address(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, dict):
        nested = value.get('emailAddress') if isinstance(value.get('emailAddress'), dict) else value
        if isinstance(nested, dict):
            for key in ('address', 'email'):
                candidate = nested.get(key)
                if candidate:
                    match = EMAIL_RE.search(str(candidate))
                    if match:
                        return match.group(0).lower()
            value = nested.get('name') or nested
        text = str(value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = extract_first_email_address(item)
            if found:
                return found
        return ''
    else:
        text = str(value)
    match = EMAIL_RE.search(text)
    return match.group(0).lower() if match else ''


def is_shopify_platform_address(value: Any) -> bool:
    address = extract_first_email_address(value)
    if not address:
        return False
    return bool(SHOPIFY_PLATFORM_DOMAIN_RE.search(address))


def looks_like_shopify_contact_form(
    sender: Any = '',
    subject: Any = '',
    body: Any = '',
) -> bool:
    haystack = f"{subject or ''}\n{body or ''}".lower()
    if not any(marker in haystack for marker in SHOPIFY_CONTACT_FORM_MARKERS):
        return False
    if is_shopify_platform_address(sender):
        return True
    return 'form-section' in haystack or 'contact form' in haystack


def extract_shopify_contact_form_email(body: Any) -> str:
    text = str(body or '')
    if not text.strip():
        return ''
    for pattern in SHOPIFY_FORM_EMAIL_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        address = extract_first_email_address(match.group(1))
        if address and not is_shopify_platform_address(address):
            return address
    return ''


def resolve_preferred_reply_address(
    *,
    sender: Any = '',
    subject: Any = '',
    body: Any = '',
    header_reply_to: Any = '',
) -> str:
    """Return a better reply recipient for Shopify contact-form notices.

    Empty string means the caller should keep using the original From.
    """
    sender_addr = extract_first_email_address(sender)
    if looks_like_shopify_contact_form(sender, subject, body):
        form_email = extract_shopify_contact_form_email(body)
        if form_email and form_email != sender_addr:
            return form_email
        header_email = extract_first_email_address(header_reply_to)
        if (
            header_email
            and header_email != sender_addr
            and not is_shopify_platform_address(header_email)
        ):
            return header_email
    return ''


def attach_preferred_reply_address(
    email_detail: Dict[str, Any],
    header_reply_to: Any = '',
) -> Dict[str, Any]:
    detail = email_detail if isinstance(email_detail, dict) else {}
    preferred = resolve_preferred_reply_address(
        sender=detail.get('from') or detail.get('sender') or '',
        subject=detail.get('subject') or '',
        body=detail.get('body') or '',
        header_reply_to=header_reply_to or detail.get('reply_to') or '',
    )
    if preferred:
        detail['reply_to'] = preferred
    return detail
