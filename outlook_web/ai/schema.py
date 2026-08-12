"""Parse and normalize structured AI analysis JSON."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List


def parse_json_text(raw_text: str, error_message: str) -> Any:
    cleaned = re.sub(r'^```(?:json)?\s*', '', str(raw_text or '').strip(), flags=re.I)
    cleaned = re.sub(r'\s*```$', '', cleaned).strip()
    try:
        return json.loads(cleaned)
    except Exception as exc:
        raise ValueError(error_message) from exc


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
