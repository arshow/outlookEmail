"""Rule matching and output guards for AI replies."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Sequence, Tuple

from outlook_web.ai.knowledge import parse_keywords

RED_TERMS = [
    'chargeback', 'lawsuit', 'fraud', 'scam', 'copyright', 'trademark', 'infringement',
    '退款纠纷', '差评', '侵权', '投诉', '欺诈', '律师函', '举报',
]
YELLOW_TERMS = [
    'refund', 'replacement', 'cancel', 'late', 'tracking', 'charge', 'password',
    '退款', '补发', '取消', '延迟', '物流', '账单', '密码', '验证码',
]

INTENT_HINTS: List[Tuple[str, List[str]]] = [
    ('refund', ['refund', '退款']),
    ('complaint', ['complaint', '投诉', '差评', 'angry']),
    ('shipping_status', ['tracking', 'ship', '物流', '发货']),
    ('delivery_delay', ['late', 'delay', 'not arrived', '延迟', '没收到']),
    ('account_access', ['password', 'login', '账号', '密码', '登录']),
    ('verification', ['verification code', 'otp', '验证码']),
    ('billing', ['invoice', 'billing', 'charge', '账单', '付款']),
]

COMMITMENT_PATTERNS = [
    re.compile(r'\b(full )?refund (is |has been )?(approved|processed)\b', re.I),
    re.compile(r'\b(definitely|guaranteed)\b', re.I),
    re.compile(r'\bwill arrive (by|on)\b', re.I),
    re.compile(r'保证到达|已经批准退款|一定可以|保证可以'),
]

SAFE_REPLY_EN = (
    'Thanks for your message. I understand your request. '
    'Let me check the details first, and I will confirm the available options with you shortly.'
)
SAFE_REPLY_ZH = '感谢您的留言。我已理解您的需求。我会先核对相关信息，稍后与您确认可行方案。'


def _max_risk(*levels: str) -> str:
    order = {'green': 0, 'yellow': 1, 'red': 2}
    best = 'green'
    for level in levels:
        candidate = str(level or 'green').lower()
        if order.get(candidate, 0) > order.get(best, 0):
            best = candidate
    return best


def parse_forbidden_phrases(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or '').strip()
    if not text:
        return []
    if text.startswith('['):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except Exception:
            pass
    return [part.strip() for part in text.replace('，', ',').split(',') if part.strip()]


def preclassify(text: str) -> Dict[str, Any]:
    haystack = str(text or '').lower()
    intent = 'unknown'
    for name, terms in INTENT_HINTS:
        if any(term in haystack for term in terms):
            intent = name
            break
    if any(term in haystack for term in RED_TERMS):
        return {
            'intent': intent,
            'risk': 'red',
            'reasons': ['检测到争议、投诉、侵权或高风险表达'],
        }
    if any(term in haystack for term in YELLOW_TERMS):
        return {
            'intent': intent,
            'risk': 'yellow',
            'reasons': ['需要人工确认事实后再回复'],
        }
    return {'intent': intent, 'risk': 'green', 'reasons': []}


def match_rules(rules: Sequence[Dict[str, Any]], text: str) -> List[Dict[str, Any]]:
    haystack = str(text or '').lower()
    pre = preclassify(haystack)
    matched = []
    for rule in rules:
        if not rule.get('enabled', True):
            continue
        if str(rule.get('status') or 'published') not in ('published', 'active', ''):
            # When loading published-only lists, status may already be filtered.
            pass
        keywords = parse_keywords(rule.get('keywords'))
        keyword_ok = (not keywords) or any(keyword in haystack for keyword in keywords)
        intents = rule.get('intents') or []
        if isinstance(intents, str):
            intents = parse_keywords(intents)
        intent_ok = (not intents) or pre['intent'] in intents or 'unknown' in intents
        if keyword_ok and intent_ok:
            matched.append(rule)
    matched.sort(key=lambda item: int(item.get('priority') or 0), reverse=True)
    return matched


def apply_output_guards(
    analysis: Dict[str, Any],
    *,
    source_text: str,
    matched_rules: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    pre = preclassify(source_text)
    rule_risk = 'green'
    if matched_rules:
        rule_risk = _max_risk(*(str(rule.get('risk_level') or rule.get('riskLevel') or 'green') for rule in matched_rules))
    risk_level = _max_risk(analysis.get('riskLevel', 'green'), pre['risk'], rule_risk)
    intent = analysis.get('intent') or 'unknown'
    if intent == 'unknown' and pre['intent'] != 'unknown':
        intent = pre['intent']

    missing_facts = list(analysis.get('missingFacts') or [])
    forbidden: List[str] = []
    for rule in matched_rules:
        forbidden.extend(parse_forbidden_phrases(rule.get('forbidden_phrases') or rule.get('forbiddenPhrases')))

    reply_text = str(analysis.get('replyText') or '').strip()
    reply_text_zh = str(analysis.get('replyTextZh') or '').strip()
    internal = str(analysis.get('internalAdviceZh') or '').strip()
    lowered = reply_text.lower()
    contains_forbidden = any(phrase.lower() in lowered for phrase in forbidden if phrase)
    contains_commitment = any(pattern.search(reply_text) for pattern in COMMITMENT_PATTERNS)
    facts_missing = bool(missing_facts)

    if contains_forbidden or (facts_missing and contains_commitment):
        reply_text = SAFE_REPLY_EN
        reply_text_zh = SAFE_REPLY_ZH
        internal = (internal + '\n安全守卫已替换包含未经确认承诺或禁止短语的草稿。').strip()

    risk_reasons = list(analysis.get('riskReasons') or [])
    for reason in pre['reasons']:
        if reason not in risk_reasons:
            risk_reasons.append(reason)

    matched_ids = list(analysis.get('matchedRuleIds') or [])
    for rule in matched_rules:
        rule_id = str(rule.get('id') or '')
        if rule_id and rule_id not in matched_ids:
            matched_ids.append(rule_id)

    return {
        **analysis,
        'intent': intent,
        'riskLevel': risk_level,
        'riskReasons': risk_reasons,
        'matchedRuleIds': matched_ids,
        'missingFacts': missing_facts,
        'internalAdviceZh': internal,
        'replyText': reply_text,
        'replyTextZh': reply_text_zh,
        'requiresHumanConfirmation': bool(
            analysis.get('requiresHumanConfirmation')
            or risk_level != 'green'
            or facts_missing
        ),
    }
