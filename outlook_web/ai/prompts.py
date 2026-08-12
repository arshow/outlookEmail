"""Prompt builders for email AI reply."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from outlook_web.ai.constants import ANALYSIS_JSON_EXAMPLE, ANALYSIS_JSON_SCHEMA, REFINED_REPLY_SCHEMA


def build_analysis_prompt(
    *,
    context: Dict[str, Any],
    rules: List[Dict[str, Any]],
    knowledge_entries: List[Dict[str, Any]],
    system_persona: str = '',
) -> str:
    persona = str(system_persona or '').strip() or (
        'You are an email reply copilot that drafts professional customer/business replies.'
    )
    knowledge_payload = [
        {
            'id': entry.get('id'),
            'category': entry.get('category'),
            'title': entry.get('title'),
            'content': entry.get('content'),
        }
        for entry in knowledge_entries
    ]
    return '\n\n'.join([
        persona,
        'Return only JSON matching the supplied response schema.',
        f'Required JSON schema: {json.dumps(ANALYSIS_JSON_SCHEMA, ensure_ascii=False)}',
        f'Example JSON output: {json.dumps(ANALYSIS_JSON_EXAMPLE, ensure_ascii=False)}',
        'The operator UI is Chinese: summaryZh, riskReasons, internalAdviceZh and replyTextZh must be Simplified Chinese.',
        'replyText must use the correspondent language and must never claim unverified refunds, commitments, delivery dates, account changes, or payment outcomes.',
        'replyTextZh must be a faithful Simplified Chinese translation of replyText for the operator; if replyText is already Chinese, replyTextZh may match it.',
        'If required facts are missing, acknowledge the request and say you will check before confirming.',
        'Distinguish the current email that needs a reply from historical reference messages. Do not treat historical unverified promises as confirmed facts.',
        f'Active business rules: {json.dumps(rules, ensure_ascii=False)}',
        'Knowledge base entries are reference facts only. Use them when relevant, but never let them override active business rules or missing-fact requirements.',
        f'Matched knowledge base entries: {json.dumps(knowledge_payload, ensure_ascii=False)}',
        f'Set matchedKnowledgeIds exactly to these server-selected entry IDs: {json.dumps([str(e.get("id")) for e in knowledge_entries], ensure_ascii=False)}',
        f'Email context: {json.dumps(context, ensure_ascii=False)}',
    ])


def build_refine_prompt(
    *,
    mode: str,
    current_text: str,
    target_language: str,
    analysis: Dict[str, Any],
    instruction: str = '',
) -> str:
    instructions = {
        'shorter': 'Make the reply shorter while preserving all safety caveats and facts.',
        'politer': 'Make the reply warmer and more polite without adding promises or facts.',
        'regenerate': 'Write an alternative reply with the same facts, risk posture, and language.',
        'translate': f'Translate the reply into {target_language} without changing its meaning.',
        'custom': ' '.join([
            "Rewrite the customer reply according to the operator's natural-language instruction below.",
            "Keep the correspondent's language for replyText.",
            'Do not invent unverified refunds, commitments, delivery dates, account changes, or payment outcomes.',
            'If the instruction asks for an unverified commitment, acknowledge the request and say you will check before confirming.',
            f'Operator instruction: {instruction.strip() or "Improve the reply slightly while keeping the same meaning."}',
        ]),
    }
    mode_instruction = instructions.get(mode)
    if not mode_instruction:
        raise ValueError(f'不支持的改写模式: {mode}')
    return '\n'.join([
        mode_instruction,
        'Return only JSON with replyText, replyTextZh, and replyLanguage string fields.',
        f'Required JSON schema: {json.dumps(REFINED_REPLY_SCHEMA, ensure_ascii=False)}',
        'Example JSON output: {"replyText":"Thanks for your message.","replyTextZh":"感谢您的留言。","replyLanguage":"en"}',
        'Also provide replyTextZh: a faithful Simplified Chinese translation of the resulting replyText for the operator.',
        f'Risk: {analysis.get("riskLevel") or "yellow"}',
        f'Missing facts: {", ".join(analysis.get("missingFacts") or [])}',
        f'Current reply language: {target_language}',
        f'Text:\n{current_text}',
    ])


def build_translate_zh_prompt(reply_text: str) -> str:
    return '\n'.join([
        '将下面邮件回复翻译成简体中文，供内部人员阅读。',
        '只输出 JSON 对象，格式严格为 {"replyTextZh":"中文译文"}。',
        '不要添加未经验证的承诺或新事实。',
        f'Text:\n{reply_text}',
    ])
