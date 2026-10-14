"""Cloudflare Workers AI Clef decision-model client.

Clef / Clef-flash return probabilities for typed questions. They do not
generate free-form text, so this module is kept separate from Gemini/DeepSeek.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional
from urllib.parse import quote

import requests

from outlook_web.ai.constants import (
    CLEF_MODEL_PATHS,
    DEFAULT_CLEF_MODEL,
    DEFAULT_CLOUDFLARE_AI_BASE_URL,
    normalize_clef_model,
)

QUESTION_ID_RE = re.compile(r'^[A-Za-z0-9_.-]{1,100}$')
CLEF_QUESTION_TYPES = ('noul', 'choice', 'score')
CLEF_MIN_QUESTIONS = 1
CLEF_MAX_QUESTIONS = 64
CLEF_PING_STATE = 'ping'
CLEF_PING_QUESTIONS = {
    'ok': {
        'type': 'noul',
        'instructions': 'Is this a connectivity test?',
    },
}


def _clef_run_url(account_id: str, model: str) -> str:
    account = quote(str(account_id or '').strip(), safe='')
    model_path = CLEF_MODEL_PATHS[model]
    root = DEFAULT_CLOUDFLARE_AI_BASE_URL.rstrip('/')
    return f'{root}/accounts/{account}/ai/run/{model_path}'


def _normalize_state(state: Any) -> Any:
    if state is None:
        raise ValueError('clef state 不能为空')
    if isinstance(state, str):
        text = state.strip()
        if not text:
            raise ValueError('clef state 不能为空')
        return text
    if isinstance(state, (dict, list, int, float, bool)):
        return state
    raise ValueError('clef state 必须是字符串或 JSON 对象/数组')


def validate_clef_questions(questions: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(questions, Mapping) or not questions:
        raise ValueError('clef questions 至少需要 1 个问题')
    if len(questions) > CLEF_MAX_QUESTIONS:
        raise ValueError(f'clef questions 最多 {CLEF_MAX_QUESTIONS} 个')

    normalized: Dict[str, Dict[str, Any]] = {}
    for raw_id, raw_question in questions.items():
        question_id = str(raw_id or '').strip()
        if not QUESTION_ID_RE.match(question_id):
            raise ValueError(f'clef question id 无效: {raw_id}')
        if not isinstance(raw_question, Mapping):
            raise ValueError(f'clef question {question_id} 必须是对象')
        qtype = str(raw_question.get('type') or '').strip().lower()
        if qtype not in CLEF_QUESTION_TYPES:
            raise ValueError(f'clef question {question_id} 的 type 必须是 noul、choice 或 score')
        item: Dict[str, Any] = {'type': qtype}
        instructions = raw_question.get('instructions')
        if instructions is not None:
            text = str(instructions).strip()
            if text:
                item['instructions'] = text
        criteria = raw_question.get('criteria')
        if qtype == 'choice':
            if not isinstance(criteria, Mapping) or not criteria:
                raise ValueError(f'clef question {question_id} 的 choice 需要非空 criteria 对象')
            item['criteria'] = {
                str(key): str(value)
                for key, value in criteria.items()
                if str(key).strip()
            }
            if not item['criteria']:
                raise ValueError(f'clef question {question_id} 的 choice 需要非空 criteria 对象')
        elif qtype == 'score':
            if not isinstance(criteria, list) or len(criteria) < 2:
                raise ValueError(f'clef question {question_id} 的 score 需要至少 2 级 criteria 列表')
            item['criteria'] = [str(value) for value in criteria]
        elif isinstance(criteria, Mapping) and criteria:
            item['criteria'] = {
                str(key): str(value)
                for key, value in criteria.items()
                if str(key).strip()
            }
        normalized[question_id] = item
    if len(normalized) < CLEF_MIN_QUESTIONS:
        raise ValueError('clef questions 至少需要 1 个问题')
    return normalized


def _cloudflare_error_text(payload: Any, status_code: int) -> str:
    messages = []
    if isinstance(payload, dict):
        errors = payload.get('errors')
        if isinstance(errors, list):
            for item in errors:
                if isinstance(item, dict):
                    text = str(item.get('message') or item.get('code') or '').strip()
                else:
                    text = str(item or '').strip()
                if text:
                    messages.append(text)
        if not messages:
            nested = payload.get('error')
            if isinstance(nested, dict):
                text = str(nested.get('message') or nested.get('code') or '').strip()
            else:
                text = str(nested or payload.get('message') or '').strip()
            if text:
                messages.append(text)
    return '；'.join(messages) or f'Cloudflare Workers AI 请求失败（HTTP {status_code}）'


def map_clef_error(message: str, status_code: int = 0) -> str:
    text = str(message or '').strip()
    lower = text.lower()
    if status_code in (401, 403) or 'authentication' in lower or 'unauthorized' in lower:
        return (
            'Cloudflare Workers AI 鉴权失败。'
            '请检查 API Token 是否具有 Workers AI Read / Edit 权限。'
        )
    if status_code == 429 or 'rate limit' in lower or 'too many requests' in lower:
        return 'Cloudflare Workers AI 请求过于频繁，请稍后再试。'
    if status_code == 404 or ('not found' in lower and 'account' in lower):
        return 'Cloudflare Workers AI 未找到账号或模型，请核对 Account ID。'
    if 'account' in lower and ('invalid' in lower or 'not found' in lower):
        return 'Cloudflare Account ID 无效或不存在。'
    return text or f'Cloudflare Workers AI 请求失败（HTTP {status_code}）'


def _unwrap_clef_result(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError('Cloudflare Workers AI 返回了无效 JSON')
    result = payload.get('result') if 'result' in payload else payload
    if isinstance(result, dict) and 'answers' not in result:
        nested = result.get('response')
        if isinstance(nested, dict) and 'answers' in nested:
            result = nested
    if not isinstance(result, dict) or 'answers' not in result:
        raise RuntimeError('Cloudflare Workers AI 未返回 answers')
    answers = result.get('answers')
    if not isinstance(answers, dict) or not answers:
        raise RuntimeError('Cloudflare Workers AI 未返回 answers')
    usage = result.get('usage')
    return {
        'model': str(result.get('model') or '').strip(),
        'answers': answers,
        'usage': usage if isinstance(usage, dict) else {},
    }


def call_clef(
    *,
    account_id: str,
    api_token: str,
    state: Any,
    questions: Mapping[str, Any],
    model: str = DEFAULT_CLEF_MODEL,
    timeout: int = 45,
) -> Dict[str, Any]:
    account = str(account_id or '').strip()
    token = str(api_token or '').strip()
    if not account:
        raise ValueError('未配置 Cloudflare Account ID')
    if not token:
        raise ValueError('未配置 Cloudflare API Token')
    model_name = normalize_clef_model(model)
    body = {
        'model': model_name,
        'state': _normalize_state(state),
        'questions': validate_clef_questions(questions),
    }
    url = _clef_run_url(account, model_name)
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }
    try:
        response = requests.post(url, headers=headers, json=body, timeout=timeout)
    except Exception as exc:
        raise RuntimeError(f'Cloudflare Workers AI 请求失败：{exc}') from exc

    try:
        payload = response.json()
    except Exception:
        payload = {}

    success_flag = payload.get('success') if isinstance(payload, dict) else None
    if response.status_code >= 400 or success_flag is False:
        raise RuntimeError(map_clef_error(_cloudflare_error_text(payload, response.status_code), response.status_code))
    return _unwrap_clef_result(payload)


def test_clef_connection(
    *,
    account_id: str,
    api_token: str,
    model: str = DEFAULT_CLEF_MODEL,
    timeout: int = 45,
) -> Dict[str, Any]:
    result = call_clef(
        account_id=account_id,
        api_token=api_token,
        model=model,
        state=CLEF_PING_STATE,
        questions=CLEF_PING_QUESTIONS,
        timeout=timeout,
    )
    return {
        'success': True,
        'provider': 'cloudflare-clef',
        'model': result.get('model') or normalize_clef_model(model),
        'answers': result.get('answers') or {},
        'usage': result.get('usage') or {},
    }


def resolve_clef_runtime(settings: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    data = settings if isinstance(settings, dict) else {}
    account_id = str(data.get('cloudflare_account_id') or '').strip()
    api_token = str(data.get('cloudflare_api_token') or '').strip()
    model = normalize_clef_model(data.get('cloudflare_model'))
    if not account_id:
        raise ValueError('未配置 Cloudflare Account ID')
    if not api_token:
        raise ValueError('未配置 Cloudflare API Token')
    return {
        'account_id': account_id,
        'api_token': api_token,
        'model': model,
    }
