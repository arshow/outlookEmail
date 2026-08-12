"""MyMemory free translation helpers (no API key)."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import requests

MYMEMORY_URL = 'https://api.mymemory.translated.net/get'
DEFAULT_MAX_CHUNK_CHARS = 450
DEFAULT_MAX_TOTAL_CHARS = 8000
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_SOURCE_LANG = 'en'
TARGET_LANG = 'zh-CN'
PROVIDER_NAME = 'mymemory'

_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?。！？\n])\s+')
_CJK_RE = re.compile(r'[\u4e00-\u9fff]')


class TranslateError(Exception):
    """User-facing translation failure."""

    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def chunk_text(text: str, max_chars: int = DEFAULT_MAX_CHUNK_CHARS) -> List[str]:
    """Split text into MyMemory-friendly chunks without dropping content."""
    normalized = str(text or '')
    if not normalized:
        return []
    if max_chars <= 0:
        raise ValueError('max_chars must be positive')

    chunks: List[str] = []
    remaining = normalized
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break

        window = remaining[:max_chars]
        split_at = -1
        for match in _SENTENCE_SPLIT_RE.finditer(window):
            split_at = match.end()
        if split_at <= 0:
            for sep in ('\n', ' ', '\t', '，', ',', ';', '；'):
                idx = window.rfind(sep)
                if idx > 0:
                    split_at = idx + 1
                    break
        if split_at <= 0:
            split_at = max_chars

        piece = remaining[:split_at]
        if not piece.strip():
            # Avoid infinite loops on whitespace-only windows.
            piece = remaining[:max_chars]
            split_at = len(piece)
        chunks.append(piece)
        remaining = remaining[split_at:]

    return [chunk for chunk in chunks if chunk]


def looks_mostly_chinese(text: str) -> bool:
    """Heuristic: skip MyMemory when source is already Simplified Chinese-heavy."""
    chars = [ch for ch in str(text or '') if not ch.isspace()]
    if not chars:
        return False
    cjk_count = sum(1 for ch in chars if _CJK_RE.match(ch))
    return (cjk_count / len(chars)) >= 0.35


def resolve_source_lang(source_lang: str) -> str:
    """MyMemory requires two distinct ISO languages; Autodetect is unreliable."""
    source = str(source_lang or DEFAULT_SOURCE_LANG).strip() or DEFAULT_SOURCE_LANG
    if source.lower() in {'auto', 'autodetect', 'detect'}:
        return DEFAULT_SOURCE_LANG
    # Normalize common aliases.
    lowered = source.lower()
    if lowered in {'zh', 'zh-cn', 'zh_cn', 'chinese'}:
        return TARGET_LANG
    if lowered in {'en-us', 'en-gb', 'english'}:
        return 'en'
    return source


def _langpair(source_lang: str) -> str:
    source = resolve_source_lang(source_lang)
    if source.lower() in {TARGET_LANG.lower(), 'zh', 'zh-cn', 'zh_cn'}:
        raise TranslateError('原文已是中文，无需翻译', status_code=400)
    return f'{source}|{TARGET_LANG}'


def _extract_translated_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise TranslateError('MyMemory 返回了无效响应')
    response_data = payload.get('responseData')
    if not isinstance(response_data, dict):
        raise TranslateError('MyMemory 返回了无效响应')
    translated = response_data.get('translatedText')
    if not isinstance(translated, str) or not translated.strip():
        raise TranslateError('MyMemory 返回了空翻译')
    return translated


def _translate_chunk(
    chunk: str,
    *,
    source_lang: str,
    timeout: int,
    session: Optional[requests.Session] = None,
) -> str:
    requester = session or requests
    try:
        response = requester.get(
            MYMEMORY_URL,
            params={
                'q': chunk,
                'langpair': _langpair(source_lang),
            },
            timeout=timeout,
        )
    except requests.Timeout as exc:
        raise TranslateError('翻译服务超时，请稍后重试', status_code=504) from exc
    except requests.RequestException as exc:
        raise TranslateError(f'无法连接翻译服务: {exc}', status_code=502) from exc

    if response.status_code == 429:
        raise TranslateError('翻译免费额度已用尽，请稍后再试', status_code=429)
    if response.status_code >= 400:
        raise TranslateError(f'翻译服务错误（HTTP {response.status_code}）', status_code=502)

    try:
        payload = response.json()
    except ValueError as exc:
        raise TranslateError('MyMemory 返回了非 JSON 响应') from exc

    status = payload.get('responseStatus')
    if status not in (None, 200, '200'):
        details = payload.get('responseDetails') or payload.get('quotaFinished')
        if status in (429, '429') or details is True:
            raise TranslateError('翻译免费额度已用尽，请稍后再试', status_code=429)
        detail_text = str(details or status or '')
        upper = detail_text.upper()
        if 'DISTINCT LANGUAGES' in upper:
            raise TranslateError('翻译失败：源语言与目标语言相同，请确认原文不是中文', status_code=400)
        if 'INVALID SOURCE LANGUAGE' in upper:
            raise TranslateError('翻译失败：不支持的源语言，请重试', status_code=400)
        raise TranslateError(f'翻译失败: {detail_text}', status_code=502)

    return _extract_translated_text(payload)


def translate_to_zh(
    text: str,
    *,
    source_lang: str = DEFAULT_SOURCE_LANG,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """Translate plain text to Simplified Chinese via MyMemory."""
    raw = str(text or '')
    if not raw.strip():
        raise TranslateError('没有可翻译的文本', status_code=400)

    if looks_mostly_chinese(raw):
        return {
            'translation': raw.strip(),
            'provider': PROVIDER_NAME,
            'truncated': False,
            'chunk_count': 0,
            'skipped': True,
        }

    truncated = False
    working = raw
    if len(working) > max_total_chars:
        working = working[:max_total_chars]
        truncated = True

    resolved_source = resolve_source_lang(source_lang)
    parts = chunk_text(working, max_chars=max_chunk_chars)
    translated_parts: List[str] = []
    for part in parts:
        if not part.strip():
            translated_parts.append(part)
            continue
        if looks_mostly_chinese(part):
            translated_parts.append(part)
            continue
        translated_parts.append(
            _translate_chunk(
                part,
                source_lang=resolved_source,
                timeout=timeout,
                session=session,
            )
        )

    return {
        'translation': ''.join(translated_parts).strip(),
        'provider': PROVIDER_NAME,
        'truncated': truncated,
        'chunk_count': len(parts),
        'skipped': False,
    }


def prepare_translate_fields(
    *,
    text: str = '',
    html: str = '',
    subject: str = '',
    html_to_plain,
) -> Dict[str, str]:
    """Normalize request fields into plain subject/body."""
    plain_body = str(text or '').strip()
    if not plain_body and html:
        plain_body = str(html_to_plain(html) or '').strip()
    subject_text = str(subject or '').strip()

    if not plain_body and not subject_text:
        raise TranslateError('text 与 html 至少提供一项有效正文', status_code=400)

    return {
        'subject': subject_text,
        'body': plain_body,
    }


def translate_email_fields_to_zh(
    *,
    subject: str = '',
    body: str = '',
    source_lang: str = DEFAULT_SOURCE_LANG,
    max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """Translate subject and/or body, respecting a shared total char budget."""
    subject_text = str(subject or '').strip()
    body_text = str(body or '').strip()
    if not subject_text and not body_text:
        raise TranslateError('没有可翻译的文本', status_code=400)

    # Placeholder subjects shown in UI should not hit the translator.
    if subject_text in {'无主题', '(无主题)', 'No Subject', 'no subject'}:
        subject_text = ''

    remaining = max_total_chars
    truncated = False
    subject_zh = ''
    body_zh = ''
    resolved_source = resolve_source_lang(source_lang)

    if subject_text:
        subject_piece = subject_text[:remaining]
        if len(subject_text) > remaining:
            truncated = True
        remaining = max(0, remaining - len(subject_piece))
        if subject_piece.strip():
            subject_result = translate_to_zh(
                subject_piece,
                source_lang=resolved_source,
                max_total_chars=len(subject_piece),
                session=session,
            )
            subject_zh = subject_result['translation']
            truncated = truncated or bool(subject_result.get('truncated'))

    if body_text:
        if remaining <= 0:
            truncated = True
        else:
            body_piece = body_text[:remaining]
            if len(body_text) > remaining:
                truncated = True
            body_result = translate_to_zh(
                body_piece,
                source_lang=resolved_source,
                max_total_chars=len(body_piece),
                session=session,
            )
            body_zh = body_result['translation']
            truncated = truncated or bool(body_result.get('truncated'))

    if subject_zh and body_zh:
        combined = f'{subject_zh}\n\n{body_zh}'
    else:
        combined = subject_zh or body_zh

    if not combined.strip():
        raise TranslateError('没有可翻译的文本', status_code=400)

    return {
        'translation': combined.strip(),
        'subject_translation': subject_zh,
        'body_translation': body_zh,
        'provider': PROVIDER_NAME,
        'truncated': truncated,
    }