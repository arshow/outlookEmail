"""Gemini / DeepSeek structured model callers."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from outlook_web.ai.constants import (
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_GEMINI_BASE_URL,
    PROVIDER_DEEPSEEK,
    PROVIDER_GEMINI,
)


def _normalize_base_url(value: str, default: str) -> str:
    text = str(value or '').strip().rstrip('/')
    return text or default


def re_search_location_unsupported(message: str) -> bool:
    import re
    return bool(re.search(r'user location is not supported', message or '', re.I))


def _parse_gemini_retry_seconds(message: str) -> Optional[int]:
    import re
    match = re.search(r'retry in\s+([\d.]+)\s*s', message or '', re.I)
    if not match:
        return None
    try:
        return max(1, int(float(match.group(1))))
    except (TypeError, ValueError):
        return None


def _parse_gemini_model_from_error(message: str) -> str:
    import re
    match = re.search(r'model:\s*([^\s,\]]+)', message or '', re.I)
    return match.group(1).strip() if match else ''


def _map_gemini_error(message: str) -> str:
    text = str(message or '').strip()
    if not text:
        return text
    if re_search_location_unsupported(text):
        return (
            'Gemini 拒绝了当前请求出口地区（User location is not supported）。'
            '请在 /ai 管理页为 Gemini 启用位于可用地区的 SOCKS5 代理。'
        )

    import re
    if re.search(r'high demand|experiencing high demand', text, re.I):
        return (
            '当前 Gemini 模型请求量过高（high demand），通常是临时拥堵，请稍后再试，'
            '或改用 gemini-2.5-flash / gemini-2.0-flash 等较稳定模型。'
        )

    if re.search(r'exceeded your current quota|quota exceeded|rate limit', text, re.I):
        model = _parse_gemini_model_from_error(text)
        retry = _parse_gemini_retry_seconds(text)
        limit_match = re.search(r'limit:\s*(\d+)', text, re.I)
        limit = limit_match.group(1) if limit_match else ''
        parts = ['Gemini API 配额或速率已达上限']
        if model:
            parts.append(f'（模型：{model}）')
        if limit:
            parts.append(f'，当前限额约 {limit} 次/分钟')
        if retry:
            parts.append(f'，建议 {retry} 秒后再试')
        parts.append('。可在 Google AI Studio 查看用量，或切换到配额更宽松的模型（如 gemini-2.5-flash）。')
        return ''.join(parts)

    return text


def socks5_proxy_url(socks5: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(socks5, dict) or not socks5.get('enabled'):
        return None
    host = str(socks5.get('hostname') or socks5.get('host') or '').strip()
    try:
        port = int(socks5.get('port') or 0)
    except (TypeError, ValueError):
        port = 0
    if not host or port <= 0:
        return None
    username = str(socks5.get('username') or '').strip()
    password = str(socks5.get('password') or '')
    auth = ''
    if username:
        auth = f'{quote(username, safe="")}:{quote(password, safe="")}@'
    return f'socks5h://{auth}{host}:{port}'


def _gemini_model_family(model: str) -> str:
    name = str(model or '').strip().lower()
    if 'gemini-3' in name or name.startswith('3.'):
        return '3'
    if '2.5' in name or 'thinking' in name:
        return '2.5'
    return 'other'


def _build_gemini_thinking_config(model: str, thinking_budget: Optional[int]) -> Optional[Dict[str, Any]]:
    if thinking_budget is None:
        return None
    family = _gemini_model_family(model)
    if family == '3':
        # 3.7 Flash rejects MINIMAL / thinkingBudget=0; LOW is the cheapest valid level.
        return {'thinkingLevel': 'low'}
    if family == '2.5':
        return {'thinkingBudget': int(thinking_budget)}
    return None


def _iter_gemini_text_parts(parts: Any):
    if not isinstance(parts, list):
        return
    for part in parts:
        if not isinstance(part, dict):
            continue
        # Gemini 2.5 thinking parts can prepend non-JSON prose.
        if part.get('thought') is True:
            continue
        text = part.get('text')
        if text:
            yield str(text)


def call_gemini(
    *,
    api_key: str,
    model: str,
    prompt: str,
    temperature: float,
    max_output_tokens: int,
    response_schema: Optional[Dict[str, Any]] = None,
    base_url: str = '',
    socks5: Optional[Dict[str, Any]] = None,
    timeout: int = 90,
    thinking_budget: Optional[int] = None,
) -> str:
    if not api_key:
        raise ValueError('未配置 Gemini API Key')
    model_name = str(model or '').strip()
    if not model_name:
        raise ValueError('未配置 Gemini 模型')

    root = _normalize_base_url(base_url, DEFAULT_GEMINI_BASE_URL)
    url = f'{root}/v1beta/models/{quote(model_name, safe="")}:generateContent'
    family = _gemini_model_family(model_name)
    generation_config: Dict[str, Any] = {
        'maxOutputTokens': max_output_tokens,
        'responseMimeType': 'application/json',
    }
    # Gemini 3.x rejects or degrades with explicit temperature / thinkingBudget.
    if family != '3':
        generation_config['temperature'] = temperature
    if response_schema:
        generation_config['responseSchema'] = response_schema
    thinking_config = _build_gemini_thinking_config(model_name, thinking_budget)
    if thinking_config:
        generation_config['thinkingConfig'] = thinking_config

    body = {
        'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
        'generationConfig': generation_config,
    }
    headers = {
        'Content-Type': 'application/json',
        'x-goog-api-key': api_key,
    }
    proxies = None
    proxy_url = socks5_proxy_url(socks5)
    if proxy_url:
        proxies = {'http': proxy_url, 'https': proxy_url}

    try:
        response = requests.post(url, headers=headers, json=body, timeout=timeout, proxies=proxies)
    except Exception as exc:
        message = str(exc)
        if proxy_url and ('SOCKS' in message.upper() or 'proxy' in message.lower()):
            raise RuntimeError(f'Gemini SOCKS5 代理失败：{message}') from exc
        raise RuntimeError(f'Gemini 请求失败：{message}') from exc

    try:
        payload = response.json()
    except Exception:
        payload = {}

    if response.status_code >= 400:
        err = ''
        if isinstance(payload, dict):
            err = str((payload.get('error') or {}).get('message') or '')
        raise RuntimeError(_map_gemini_error(err or f'Gemini 请求失败（HTTP {response.status_code}）'))

    candidates = payload.get('candidates') if isinstance(payload, dict) else None
    text_parts = []
    finish_reason = ''
    if isinstance(candidates, list) and candidates:
        first = candidates[0] or {}
        finish_reason = str(first.get('finishReason') or '')
        parts = ((first.get('content') or {}).get('parts') or [])
        text_parts.extend(_iter_gemini_text_parts(parts))
    text = ''.join(text_parts).strip()
    if not text:
        raise RuntimeError(f'Gemini 未返回内容（{finish_reason or "empty_candidates"}）')
    return text


def call_deepseek(
    *,
    api_key: str,
    model: str,
    prompt: str,
    temperature: float,
    max_output_tokens: int,
    base_url: str = '',
    timeout: int = 90,
) -> str:
    if not api_key:
        raise ValueError('未配置 DeepSeek API Key')
    model_name = str(model or '').strip()
    if not model_name:
        raise ValueError('未配置 DeepSeek 模型')

    root = _normalize_base_url(base_url, DEFAULT_DEEPSEEK_BASE_URL)
    url = f'{root}/chat/completions'
    body = {
        'model': model_name,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': temperature,
        'max_tokens': max_output_tokens,
        'response_format': {'type': 'json_object'},
        'stream': False,
    }
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
    }
    try:
        response = requests.post(url, headers=headers, json=body, timeout=timeout)
    except Exception as exc:
        raise RuntimeError(f'DeepSeek 请求失败：{exc}') from exc

    try:
        payload = response.json()
    except Exception:
        payload = {}

    if response.status_code >= 400:
        err = ''
        if isinstance(payload, dict):
            err = str((payload.get('error') or {}).get('message') or '')
        raise RuntimeError(err or f'DeepSeek 请求失败（HTTP {response.status_code}）')

    choices = payload.get('choices') if isinstance(payload, dict) else None
    text = ''
    finish_reason = ''
    if isinstance(choices, list) and choices:
        first = choices[0] or {}
        finish_reason = str(first.get('finish_reason') or '')
        message = first.get('message') or {}
        text = str(message.get('content') or '').strip()
    if not text:
        raise RuntimeError(f'DeepSeek 未返回内容（{finish_reason or "empty_choices"}）')
    return text


def call_structured_model(
    *,
    provider: str,
    api_key: str,
    model: str,
    prompt: str,
    temperature: float = 0.2,
    max_output_tokens: int = 4096,
    response_schema: Optional[Dict[str, Any]] = None,
    gemini_base_url: str = '',
    deepseek_base_url: str = '',
    gemini_socks5: Optional[Dict[str, Any]] = None,
    thinking_budget: Optional[int] = None,
) -> str:
    provider_name = str(provider or '').strip().lower()
    if provider_name == PROVIDER_DEEPSEEK:
        return call_deepseek(
            api_key=api_key,
            model=model,
            prompt=prompt,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            base_url=deepseek_base_url,
        )
    if provider_name == PROVIDER_GEMINI:
        return call_gemini(
            api_key=api_key,
            model=model,
            prompt=prompt,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            response_schema=response_schema,
            base_url=gemini_base_url,
            socks5=gemini_socks5,
            thinking_budget=thinking_budget,
        )
    raise ValueError(f'不支持的 AI 提供商: {provider}')


def _strip_gemini_model_name(name: str) -> str:
    text = str(name or '').strip()
    if text.startswith('models/'):
        return text[len('models/'):]
    return text


def list_gemini_models(
    *,
    api_key: str,
    base_url: str = '',
    socks5: Optional[Dict[str, Any]] = None,
    timeout: int = 45,
) -> List[Dict[str, Any]]:
    if not api_key:
        raise ValueError('未配置 Gemini API Key')
    root = _normalize_base_url(base_url, DEFAULT_GEMINI_BASE_URL)
    url = f'{root}/v1beta/models'
    headers = {
        'Content-Type': 'application/json',
        'x-goog-api-key': api_key,
    }
    proxies = None
    proxy_url = socks5_proxy_url(socks5)
    if proxy_url:
        proxies = {'http': proxy_url, 'https': proxy_url}

    try:
        response = requests.get(url, headers=headers, timeout=timeout, proxies=proxies, params={'pageSize': 1000})
    except Exception as exc:
        message = str(exc)
        if proxy_url and ('SOCKS' in message.upper() or 'proxy' in message.lower()):
            raise RuntimeError(f'Gemini SOCKS5 代理失败：{message}') from exc
        raise RuntimeError(f'Gemini 读取模型失败：{message}') from exc

    try:
        payload = response.json()
    except Exception:
        payload = {}

    if response.status_code >= 400:
        err = ''
        if isinstance(payload, dict):
            err = str((payload.get('error') or {}).get('message') or '')
        raise RuntimeError(_map_gemini_error(err or f'Gemini 读取模型失败（HTTP {response.status_code}）'))

    models: List[Dict[str, Any]] = []
    raw_models = payload.get('models') if isinstance(payload, dict) else None
    if isinstance(raw_models, list):
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            methods = item.get('supportedGenerationMethods') or []
            if isinstance(methods, list) and methods and 'generateContent' not in methods:
                continue
            model_id = _strip_gemini_model_name(str(item.get('name') or ''))
            if not model_id:
                continue
            models.append({
                'id': model_id,
                'display_name': str(item.get('displayName') or model_id),
                'description': str(item.get('description') or ''),
            })
    models.sort(key=lambda row: str(row.get('id') or '').lower())
    return models


def list_deepseek_models(
    *,
    api_key: str,
    base_url: str = '',
    timeout: int = 45,
) -> List[Dict[str, Any]]:
    if not api_key:
        raise ValueError('未配置 DeepSeek API Key')
    root = _normalize_base_url(base_url, DEFAULT_DEEPSEEK_BASE_URL)
    url = f'{root}/models'
    headers = {
        'Authorization': f'Bearer {api_key}',
    }
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except Exception as exc:
        raise RuntimeError(f'DeepSeek 读取模型失败：{exc}') from exc

    try:
        payload = response.json()
    except Exception:
        payload = {}

    if response.status_code >= 400:
        err = ''
        if isinstance(payload, dict):
            err = str((payload.get('error') or {}).get('message') or '')
        raise RuntimeError(err or f'DeepSeek 读取模型失败（HTTP {response.status_code}）')

    models: List[Dict[str, Any]] = []
    raw_models = payload.get('data') if isinstance(payload, dict) else None
    if isinstance(raw_models, list):
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get('id') or '').strip()
            if not model_id:
                continue
            models.append({
                'id': model_id,
                'display_name': model_id,
                'description': str(item.get('owned_by') or ''),
            })
    models.sort(key=lambda row: str(row.get('id') or '').lower())
    return models


def _resolve_socks5_from_settings(settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    socks = settings.get('gemini_socks5_full')
    if isinstance(socks, dict):
        return socks
    socks = settings.get('gemini_socks5')
    if isinstance(socks, dict):
        return socks
    return None


def list_available_models(settings: Dict[str, Any], provider: Optional[str] = None) -> Dict[str, Any]:
    provider_name = str(provider or settings.get('provider') or PROVIDER_GEMINI).strip().lower()
    if provider_name == PROVIDER_GEMINI:
        models = list_gemini_models(
            api_key=str(settings.get('gemini_api_key') or ''),
            base_url=str(settings.get('gemini_base_url') or ''),
            socks5=_resolve_socks5_from_settings(settings),
        )
    elif provider_name == PROVIDER_DEEPSEEK:
        models = list_deepseek_models(
            api_key=str(settings.get('deepseek_api_key') or ''),
            base_url=str(settings.get('deepseek_base_url') or ''),
        )
    else:
        raise ValueError(f'不支持的 AI 提供商: {provider_name}')
    return {
        'success': True,
        'provider': provider_name,
        'models': models,
        'count': len(models),
    }


def test_provider_connection(settings: Dict[str, Any], provider: Optional[str] = None) -> Dict[str, Any]:
    provider_name = str(provider or settings.get('provider') or PROVIDER_GEMINI).strip().lower()
    prompt = (
        'Return only JSON object {"ok":true,"provider":"'
        + provider_name
        + '"} with no extra text.'
    )
    schema = {
        'type': 'object',
        'required': ['ok', 'provider'],
        'properties': {
            'ok': {'type': 'boolean'},
            'provider': {'type': 'string'},
        },
    }
    if provider_name == PROVIDER_GEMINI:
        api_key = str(settings.get('gemini_api_key') or '')
        model = str(settings.get('model') or '')
        text = call_gemini(
            api_key=api_key,
            model=model,
            prompt=prompt,
            temperature=0,
            max_output_tokens=64,
            response_schema=schema,
            base_url=str(settings.get('gemini_base_url') or ''),
            socks5=_resolve_socks5_from_settings(settings),
            timeout=45,
            thinking_budget=0,
        )
    elif provider_name == PROVIDER_DEEPSEEK:
        api_key = str(settings.get('deepseek_api_key') or '')
        model = str(settings.get('model') or '')
        text = call_deepseek(
            api_key=api_key,
            model=model,
            prompt=prompt,
            temperature=0,
            max_output_tokens=64,
            base_url=str(settings.get('deepseek_base_url') or ''),
            timeout=45,
        )
    else:
        raise ValueError(f'不支持的 AI 提供商: {provider_name}')

    try:
        parsed = json.loads(text)
    except Exception:
        parsed = {'raw': text}
    return {'success': True, 'provider': provider_name, 'model': settings.get('model'), 'response': parsed}
