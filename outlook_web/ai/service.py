"""AI analyze / refine orchestration."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, Optional

from outlook_web.ai.constants import (
    ANALYSIS_JSON_SCHEMA,
    CONTEXT_SCOPE_CURRENT,
    CONTEXT_SCOPES,
    REFINED_REPLY_SCHEMA,
    REFINE_MODES,
)
from outlook_web.ai.context import build_analysis_context, context_haystack
from outlook_web.ai.db import get_knowledge_revision, list_knowledge_entries, list_published_rules
from outlook_web.ai.knowledge import match_knowledge_entries
from outlook_web.ai.llm import call_structured_model
from outlook_web.ai.prompts import build_analysis_prompt, build_refine_prompt, build_translate_zh_prompt
from outlook_web.ai.rules import apply_output_guards, match_rules
from outlook_web.ai.schema import normalize_analysis, normalize_refined_reply, parse_json_text
from outlook_web.ai.settings import resolve_runtime_credentials


def _fingerprint(*parts: Any) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _translate_reply_zh(credentials: Dict[str, Any], reply_text: str) -> str:
    prompt = build_translate_zh_prompt(reply_text)
    text = call_structured_model(
        provider=credentials['provider'],
        api_key=credentials['api_key'],
        model=credentials['model'],
        prompt=prompt,
        temperature=0.1,
        max_output_tokens=1200,
        response_schema={
            'type': 'object',
            'required': ['replyTextZh'],
            'properties': {'replyTextZh': {'type': 'string'}},
        },
        gemini_base_url=credentials['gemini_base_url'],
        deepseek_base_url=credentials['deepseek_base_url'],
        gemini_socks5=credentials.get('gemini_socks5'),
    )
    parsed = parse_json_text(text, f"{credentials['provider']} 返回了无效翻译 JSON")
    if isinstance(parsed, dict):
        for key in ('replyTextZh', 'translation', 'chinese', 'zh', 'text'):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    raise ValueError(f"{credentials['provider']} 返回了空的中文翻译")


def _load_latest_run(db, account_email: str, message_id: str, context_scope: str, fingerprint: str):
    row = db.execute(
        '''
        SELECT * FROM ai_analysis_runs
        WHERE account_email = ?
          AND message_id = ?
          AND context_scope = ?
          AND input_fingerprint = ?
          AND status = 'succeeded'
        ORDER BY id DESC
        LIMIT 1
        ''',
        (account_email, message_id, context_scope, fingerprint),
    ).fetchone()
    return dict(row) if row else None


def analyze_email(
    *,
    db,
    settings: Dict[str, Any],
    account_email: str,
    account_id: int,
    message_id: str,
    current_detail: Dict[str, Any],
    context_scope: str = CONTEXT_SCOPE_CURRENT,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    if not settings.get('enabled'):
        raise ValueError('AI 智能回复未启用，请先在 /ai 配置并开启')

    scope = str(context_scope or CONTEXT_SCOPE_CURRENT).strip().lower()
    if scope not in CONTEXT_SCOPES:
        raise ValueError('context_scope 必须是 current 或 contact_local')

    credentials = resolve_runtime_credentials(settings)
    context, meta = build_analysis_context(
        scope=scope,
        current_detail=current_detail,
        account_email=account_email,
        account_id=account_id,
        db=db,
    )
    haystack = context_haystack(context)
    knowledge_entries = match_knowledge_entries(list_knowledge_entries(db, include_disabled=False), haystack)
    published_rules = list_published_rules(db)
    matched_rules = match_rules(published_rules, haystack)
    knowledge_revision = get_knowledge_revision(db)
    fingerprint = _fingerprint(
        credentials['provider'],
        credentials['model'],
        meta['context_scope'],
        meta['history_count'],
        knowledge_revision,
        [rule.get('id') for rule in matched_rules],
        [entry.get('id') for entry in knowledge_entries],
        context.get('currentEmail'),
        [(m.get('id'), m.get('body_text')) for m in (context.get('historyMessages') or [])],
    )

    if not force_refresh:
        cached = _load_latest_run(db, account_email, message_id, meta['context_scope'], fingerprint)
        if cached and cached.get('result_json'):
            try:
                cached_result = json.loads(cached['result_json'])
                return {
                    'success': True,
                    'cached': True,
                    'run_id': cached['id'],
                    'analysis': cached_result.get('analysis'),
                    'meta': cached_result.get('meta') or meta,
                    'provider': cached.get('provider'),
                    'model': cached.get('model'),
                }
            except Exception:
                pass

    prompt = build_analysis_prompt(
        context=context,
        rules=[
            {
                'id': str(rule.get('id')),
                'keywords': rule.get('keywords'),
                'instruction': rule.get('instruction'),
                'forbiddenPhrases': rule.get('forbidden_phrases'),
                'riskLevel': rule.get('risk_level'),
                'priority': rule.get('priority'),
            }
            for rule in matched_rules
        ],
        knowledge_entries=knowledge_entries,
        system_persona=credentials.get('system_persona') or '',
    )

    started = time.time()
    status = 'succeeded'
    error_message = None
    analysis = None
    try:
        raw_text = call_structured_model(
            provider=credentials['provider'],
            api_key=credentials['api_key'],
            model=credentials['model'],
            prompt=prompt,
            temperature=0.2,
            max_output_tokens=4096,
            response_schema=ANALYSIS_JSON_SCHEMA,
            gemini_base_url=credentials['gemini_base_url'],
            deepseek_base_url=credentials['deepseek_base_url'],
            gemini_socks5=credentials.get('gemini_socks5'),
        )
        parsed = parse_json_text(raw_text, f"{credentials['provider']} 返回了无效分析 JSON")
        analysis = normalize_analysis(parsed)
        analysis['matchedKnowledgeIds'] = [str(entry.get('id')) for entry in knowledge_entries]
        analysis = apply_output_guards(analysis, source_text=haystack, matched_rules=matched_rules)
        if not analysis.get('replyTextZh'):
            analysis['replyTextZh'] = _translate_reply_zh(credentials, analysis['replyText'])
    except Exception as exc:
        status = 'failed'
        error_message = str(exc)
        duration_ms = int((time.time() - started) * 1000)
        cursor = db.execute(
            '''
            INSERT INTO ai_analysis_runs (
                account_email, message_id, context_scope, provider, model,
                input_fingerprint, status, error_message, duration_ms,
                history_count, contact_email, result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                account_email,
                message_id,
                meta['context_scope'],
                credentials['provider'],
                credentials['model'],
                fingerprint,
                status,
                error_message,
                duration_ms,
                meta['history_count'],
                meta.get('contact_email') or '',
                None,
            ),
        )
        db.commit()
        raise

    duration_ms = int((time.time() - started) * 1000)
    result_payload = {'analysis': analysis, 'meta': meta}
    cursor = db.execute(
        '''
        INSERT INTO ai_analysis_runs (
            account_email, message_id, context_scope, provider, model,
            input_fingerprint, status, error_message, duration_ms,
            history_count, contact_email, result_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            account_email,
            message_id,
            meta['context_scope'],
            credentials['provider'],
            credentials['model'],
            fingerprint,
            status,
            error_message,
            duration_ms,
            meta['history_count'],
            meta.get('contact_email') or '',
            json.dumps(result_payload, ensure_ascii=False),
        ),
    )
    run_id = cursor.lastrowid
    db.execute(
        '''
        INSERT INTO ai_drafts (run_id, kind, reply_text, reply_text_zh)
        VALUES (?, 'initial', ?, ?)
        ''',
        (run_id, analysis['replyText'], analysis.get('replyTextZh') or ''),
    )
    db.commit()
    return {
        'success': True,
        'cached': False,
        'run_id': run_id,
        'analysis': analysis,
        'meta': meta,
        'provider': credentials['provider'],
        'model': credentials['model'],
        'duration_ms': duration_ms,
    }


def refine_reply(
    *,
    settings: Dict[str, Any],
    current_text: str,
    mode: str,
    analysis: Optional[Dict[str, Any]] = None,
    instruction: str = '',
    target_language: str = '',
    db=None,
    run_id: Optional[int] = None,
) -> Dict[str, Any]:
    if not settings.get('enabled'):
        raise ValueError('AI 智能回复未启用，请先在 /ai 配置并开启')

    mode_name = str(mode or '').strip().lower()
    if mode_name not in REFINE_MODES:
        raise ValueError(f'不支持的改写模式: {mode}')

    credentials = resolve_runtime_credentials(settings)
    source = str(current_text or '').strip()
    if not source:
        raise ValueError('没有可改写的正文')

    analysis = analysis or {}
    language = str(target_language or analysis.get('replyLanguage') or 'unknown')

    if mode_name == 'translate_zh':
        reply_text_zh = _translate_reply_zh(credentials, source)
        result = {
            'replyText': source,
            'replyTextZh': reply_text_zh,
            'replyLanguage': language,
        }
    else:
        prompt = build_refine_prompt(
            mode=mode_name,
            current_text=source,
            target_language=language,
            analysis=analysis,
            instruction=instruction,
        )
        raw_text = call_structured_model(
            provider=credentials['provider'],
            api_key=credentials['api_key'],
            model=credentials['model'],
            prompt=prompt,
            temperature=0.2,
            max_output_tokens=1600,
            response_schema=REFINED_REPLY_SCHEMA,
            gemini_base_url=credentials['gemini_base_url'],
            deepseek_base_url=credentials['deepseek_base_url'],
            gemini_socks5=credentials.get('gemini_socks5'),
        )
        parsed = parse_json_text(raw_text, f"{credentials['provider']} 返回了无效改写 JSON")
        result = normalize_refined_reply(parsed, fallback_language=language)
        if not result.get('replyTextZh'):
            result['replyTextZh'] = _translate_reply_zh(credentials, result['replyText'])

    if db is not None and run_id:
        db.execute(
            '''
            INSERT INTO ai_drafts (run_id, kind, reply_text, reply_text_zh)
            VALUES (?, ?, ?, ?)
            ''',
            (run_id, f'refined:{mode_name}', result['replyText'], result.get('replyTextZh') or ''),
        )
        db.commit()

    return {
        'success': True,
        'reply': result,
        'provider': credentials['provider'],
        'model': credentials['model'],
    }
