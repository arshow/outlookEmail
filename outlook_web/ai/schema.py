"""Parse and normalize structured AI analysis JSON."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional


def _strip_code_fence(text: str) -> str:
    cleaned = str(text or '').strip()
    if cleaned.startswith('```'):
        cleaned = re.sub(r'^```(?:json|javascript)?\s*', '', cleaned, count=1, flags=re.I)
        cleaned = re.sub(r'\s*```(?:\w*)?\s*$', '', cleaned)
    return cleaned.strip()


def _extract_json_blob(text: str) -> str:
    cleaned = _strip_code_fence(text)
    start_obj = cleaned.find('{')
    start_arr = cleaned.find('[')
    starts = [index for index in (start_obj, start_arr) if index >= 0]
    if not starts:
        return cleaned
    start = min(starts)
    opener = cleaned[start]
    closer = '}' if opener == '{' else ']'
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(cleaned[start:], start):
        if in_string:
            if escape:
                escape = False
            elif char == '\\':
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return cleaned[start:index + 1]
    return cleaned[start:]


def _escape_raw_controls_in_strings(text: str) -> str:
    """Turn raw newlines/tabs inside JSON strings into valid escapes."""
    result: List[str] = []
    in_string = False
    escape = False
    for char in text:
        if in_string:
            if escape:
                result.append(char)
                escape = False
                continue
            if char == '\\':
                result.append(char)
                escape = True
                continue
            if char == '"':
                in_string = False
                result.append(char)
                continue
            if char == '\n':
                result.append('\\n')
                continue
            if char == '\r':
                continue
            if char == '\t':
                result.append('\\t')
                continue
            if ord(char) < 32:
                result.append(f'\\u{ord(char):04x}')
                continue
            result.append(char)
            continue
        if char == '"':
            in_string = True
        result.append(char)
    return ''.join(result)


def _strip_trailing_commas(text: str) -> str:
    return re.sub(r',\s*([}\]])', r'\1', text)


def _close_truncated_json(text: str) -> str:
    blob = str(text or '').rstrip()
    if not blob:
        return blob
    in_string = False
    escape = False
    stack: List[str] = []
    for char in blob:
        if in_string:
            if escape:
                escape = False
            elif char == '\\':
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == '{':
            stack.append('}')
        elif char == '[':
            stack.append(']')
        elif char in '}]' and stack:
            stack.pop()
    suffix: List[str] = []
    if in_string:
        suffix.append('"')
    suffix.extend(reversed(stack))
    return blob + ''.join(suffix) if suffix else blob


def parse_json_text(raw_text: str, error_message: str) -> Any:
    original = str(raw_text or '').strip()
    if not original:
        raise ValueError(error_message)

    blobs: List[str] = []
    for text in (original, _strip_code_fence(original), _extract_json_blob(original)):
        if text and text not in blobs:
            blobs.append(text)

    last_exc: Optional[Exception] = None
    tried = set()
    for blob in blobs:
        variants = [
            blob,
            _escape_raw_controls_in_strings(blob),
        ]
        variants.append(_strip_trailing_commas(variants[-1]))
        variants.append(_close_truncated_json(variants[-1]))
        for variant in variants:
            if variant in tried:
                continue
            tried.add(variant)
            try:
                return json.loads(variant)
            except Exception as exc:
                last_exc = exc
    raise ValueError(error_message) from last_exc


def _as_str_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = str(item or '').strip()
        if text:
            result.append(text)
    return result


def normalize_analysis(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError('分析 JSON 必须是对象')

    reply_text = str(payload.get('replyText') or '').strip()
    if not reply_text:
        raise ValueError('分析 JSON 缺少 replyText')

    risk = str(payload.get('riskLevel') or 'yellow').strip().lower()
    if risk not in ('green', 'yellow', 'red'):
        risk = 'yellow'

    confidence_raw = payload.get('confidence', 0.5)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    requires = payload.get('requiresHumanConfirmation')
    if not isinstance(requires, bool):
        requires = risk != 'green'

    return {
        'sourceLanguage': str(payload.get('sourceLanguage') or '').strip() or 'unknown',
        'summaryZh': str(payload.get('summaryZh') or '').strip(),
        'intent': str(payload.get('intent') or 'unknown').strip() or 'unknown',
        'riskLevel': risk,
        'riskReasons': _as_str_list(payload.get('riskReasons')),
        'matchedRuleIds': _as_str_list(payload.get('matchedRuleIds')),
        'matchedKnowledgeIds': _as_str_list(payload.get('matchedKnowledgeIds')),
        'missingFacts': _as_str_list(payload.get('missingFacts')),
        'internalAdviceZh': str(payload.get('internalAdviceZh') or '').strip(),
        'replyLanguage': str(payload.get('replyLanguage') or '').strip() or 'unknown',
        'replyText': reply_text,
        'replyTextZh': str(payload.get('replyTextZh') or '').strip(),
        'confidence': confidence,
        'requiresHumanConfirmation': requires,
    }


def normalize_refined_reply(payload: Any, fallback_language: str = 'unknown') -> Dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError('改写 JSON 必须是对象')
    reply_text = str(payload.get('replyText') or '').strip()
    if not reply_text:
        raise ValueError('改写 JSON 缺少 replyText')
    return {
        'replyText': reply_text,
        'replyTextZh': str(payload.get('replyTextZh') or '').strip(),
        'replyLanguage': str(payload.get('replyLanguage') or fallback_language).strip() or fallback_language,
    }
