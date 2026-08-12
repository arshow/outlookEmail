"""Gemini / DeepSeek structured model callers."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional
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


def _map_gemini_error(message: str) -> str:
    if re_search_location_unsupported(message):
        return (
            'Gemini 拒绝了当前请求出口地区（User location is not supported）。'
            '请在 /ai 管理页为 Gemini 启用位于可用地区的 SOCKS5 代理。'
        )
    return message


def re_search_location_unsupported(message: str) -> bool:
    import re
    return bool(re.search(r'user location is not supported', message or '', re.I))


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
) -> str:
    if not api_key:
        raise ValueError('未配置 Gemini API Key')
    model_name = str(model or '').strip()
    if not model_name:
        raise ValueError('未配置 Gemini 模型')

    root = _normalize_base_url(base_url, DEFAULT_GEMINI_BASE_URL)
    url = f'{root}/v1beta/models/{quote(model_name, safe="")}:generateContent'
    generation_config: Dict[str, Any] = {
        'temperature': temperature,
        'maxOutputTokens': max_output_tokens,
        'responseMimeType': 'application/json',
    }
    if response_schema:
        generation_config['responseSchema'] = response_schema

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
        for part in parts:
            if isinstance(part, dict) and part.get('text'):
                text_parts.append(str(part['text']))
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
        )
    raise ValueError(f'不支持的 AI 提供商: {provider}')


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
            socks5=settings.get('gemini_socks5') if isinstance(settings.get('gemini_socks5'), dict) else None,
            timeout=45,
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
