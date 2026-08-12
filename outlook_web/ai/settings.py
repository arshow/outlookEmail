"""AI reply settings helpers (backed by app settings table)."""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

from outlook_web.ai.constants import (
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_GEMINI_BASE_URL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_PROVIDER,
    PROVIDER_DEEPSEEK,
    PROVIDER_GEMINI,
    PROVIDERS,
    SETTING_DEEPSEEK_API_KEY,
    SETTING_DEEPSEEK_BASE_URL,
    SETTING_ENABLED,
    SETTING_GEMINI_API_KEY,
    SETTING_GEMINI_BASE_URL,
    SETTING_GEMINI_SOCKS5,
    SETTING_MODEL,
    SETTING_PROVIDER,
    SETTING_SYSTEM_PERSONA,
)


def _truthy(value: Any) -> bool:
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')


def parse_socks5(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        data = value
    else:
        text = str(value or '').strip()
        if not text:
            return {
                'enabled': False,
                'hostname': '',
                'port': 0,
                'username': '',
                'password': '',
                'has_password': False,
            }
        try:
            data = json.loads(text)
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    password = str(data.get('password') or '')
    try:
        port = int(data.get('port') or 0)
    except (TypeError, ValueError):
        port = 0
    return {
        'enabled': bool(data.get('enabled')),
        'hostname': str(data.get('hostname') or data.get('host') or '').strip(),
        'port': port,
        'username': str(data.get('username') or '').strip(),
        'password': password,
        'has_password': bool(password),
    }


def get_ai_reply_settings(
    *,
    get_setting: Callable[[str, str], str],
    get_setting_decrypted: Callable[[str, str], str],
) -> Dict[str, Any]:
    provider = str(get_setting(SETTING_PROVIDER, DEFAULT_PROVIDER) or DEFAULT_PROVIDER).strip().lower()
    if provider not in PROVIDERS:
        provider = DEFAULT_PROVIDER
    model = str(get_setting(SETTING_MODEL, '') or '').strip()
    if not model:
        model = DEFAULT_GEMINI_MODEL if provider == PROVIDER_GEMINI else DEFAULT_DEEPSEEK_MODEL

    gemini_key = get_setting_decrypted(SETTING_GEMINI_API_KEY, '') or ''
    deepseek_key = get_setting_decrypted(SETTING_DEEPSEEK_API_KEY, '') or ''
    socks = parse_socks5(get_setting_decrypted(SETTING_GEMINI_SOCKS5, '') or get_setting(SETTING_GEMINI_SOCKS5, ''))

    public_socks = {
        'enabled': socks['enabled'],
        'hostname': socks['hostname'],
        'port': socks['port'],
        'username': socks['username'],
        'has_password': socks['has_password'],
    }

    return {
        'enabled': _truthy(get_setting(SETTING_ENABLED, 'false')),
        'provider': provider,
        'model': model,
        'gemini_base_url': str(get_setting(SETTING_GEMINI_BASE_URL, DEFAULT_GEMINI_BASE_URL) or DEFAULT_GEMINI_BASE_URL).strip(),
        'deepseek_base_url': str(get_setting(SETTING_DEEPSEEK_BASE_URL, DEFAULT_DEEPSEEK_BASE_URL) or DEFAULT_DEEPSEEK_BASE_URL).strip(),
        'system_persona': str(get_setting(SETTING_SYSTEM_PERSONA, '') or ''),
        'gemini_api_key_configured': bool(gemini_key),
        'deepseek_api_key_configured': bool(deepseek_key),
        'gemini_api_key_masked': '********' if gemini_key else '',
        'deepseek_api_key_masked': '********' if deepseek_key else '',
        'gemini_socks5': public_socks,
        # Internal secrets for callers that need them (routes strip before JSON).
        'gemini_api_key': gemini_key,
        'deepseek_api_key': deepseek_key,
        'gemini_socks5_full': socks,
    }


def public_ai_reply_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'enabled': bool(settings.get('enabled')),
        'provider': settings.get('provider'),
        'model': settings.get('model'),
        'gemini_base_url': settings.get('gemini_base_url'),
        'deepseek_base_url': settings.get('deepseek_base_url'),
        'system_persona': settings.get('system_persona'),
        'gemini_api_key_configured': bool(settings.get('gemini_api_key_configured')),
        'deepseek_api_key_configured': bool(settings.get('deepseek_api_key_configured')),
        'gemini_api_key_masked': settings.get('gemini_api_key_masked') or '',
        'deepseek_api_key_masked': settings.get('deepseek_api_key_masked') or '',
        'gemini_socks5': settings.get('gemini_socks5') or {
            'enabled': False,
            'hostname': '',
            'port': 0,
            'username': '',
            'has_password': False,
        },
    }


def resolve_runtime_credentials(settings: Dict[str, Any], provider: Optional[str] = None) -> Dict[str, Any]:
    provider_name = str(provider or settings.get('provider') or DEFAULT_PROVIDER).strip().lower()
    if provider_name not in PROVIDERS:
        raise ValueError('无效的 AI 提供商')
    model = str(settings.get('model') or '').strip()
    if not model:
        model = DEFAULT_GEMINI_MODEL if provider_name == PROVIDER_GEMINI else DEFAULT_DEEPSEEK_MODEL
    if provider_name == PROVIDER_GEMINI:
        api_key = str(settings.get('gemini_api_key') or '')
        if not api_key:
            raise ValueError('未配置 Gemini API Key')
    else:
        api_key = str(settings.get('deepseek_api_key') or '')
        if not api_key:
            raise ValueError('未配置 DeepSeek API Key')
    return {
        'provider': provider_name,
        'model': model,
        'api_key': api_key,
        'gemini_base_url': settings.get('gemini_base_url') or DEFAULT_GEMINI_BASE_URL,
        'deepseek_base_url': settings.get('deepseek_base_url') or DEFAULT_DEEPSEEK_BASE_URL,
        'gemini_socks5': settings.get('gemini_socks5_full') or parse_socks5(settings.get('gemini_socks5')),
        'system_persona': settings.get('system_persona') or '',
    }


def save_ai_reply_settings(
    data: Dict[str, Any],
    *,
    get_setting: Callable[[str, str], str],
    get_setting_decrypted: Callable[[str, str], str],
    set_setting: Callable[[str, str], bool],
    set_setting_encrypted: Callable[[str, str], bool],
) -> Dict[str, Any]:
    updated = []

    if 'enabled' in data:
        value = 'true' if _truthy(data.get('enabled')) else 'false'
        if set_setting(SETTING_ENABLED, value):
            updated.append(SETTING_ENABLED)

    if 'provider' in data:
        provider = str(data.get('provider') or '').strip().lower()
        if provider not in PROVIDERS:
            raise ValueError('provider 必须是 gemini 或 deepseek')
        if set_setting(SETTING_PROVIDER, provider):
            updated.append(SETTING_PROVIDER)

    if 'model' in data:
        model = str(data.get('model') or '').strip()
        if set_setting(SETTING_MODEL, model):
            updated.append(SETTING_MODEL)

    if 'gemini_base_url' in data:
        if set_setting(SETTING_GEMINI_BASE_URL, str(data.get('gemini_base_url') or '').strip()):
            updated.append(SETTING_GEMINI_BASE_URL)

    if 'deepseek_base_url' in data:
        if set_setting(SETTING_DEEPSEEK_BASE_URL, str(data.get('deepseek_base_url') or '').strip()):
            updated.append(SETTING_DEEPSEEK_BASE_URL)

    if 'system_persona' in data:
        if set_setting(SETTING_SYSTEM_PERSONA, str(data.get('system_persona') or '')):
            updated.append(SETTING_SYSTEM_PERSONA)

    if data.get('clear_gemini_api_key'):
        if set_setting_encrypted(SETTING_GEMINI_API_KEY, ''):
            updated.append(SETTING_GEMINI_API_KEY)
    elif 'gemini_api_key' in data:
        key = str(data.get('gemini_api_key') or '').strip()
        if key:
            if set_setting_encrypted(SETTING_GEMINI_API_KEY, key):
                updated.append(SETTING_GEMINI_API_KEY)

    if data.get('clear_deepseek_api_key'):
        if set_setting_encrypted(SETTING_DEEPSEEK_API_KEY, ''):
            updated.append(SETTING_DEEPSEEK_API_KEY)
    elif 'deepseek_api_key' in data:
        key = str(data.get('deepseek_api_key') or '').strip()
        if key:
            if set_setting_encrypted(SETTING_DEEPSEEK_API_KEY, key):
                updated.append(SETTING_DEEPSEEK_API_KEY)

    if 'gemini_socks5' in data:
        incoming = data.get('gemini_socks5')
        current = parse_socks5(get_setting_decrypted(SETTING_GEMINI_SOCKS5, '') or get_setting(SETTING_GEMINI_SOCKS5, ''))
        parsed = parse_socks5(incoming)
        # Preserve password when UI sends empty password and has_password was true.
        if isinstance(incoming, dict) and not str(incoming.get('password') or '') and current.get('password'):
            if incoming.get('keep_password', True):
                parsed['password'] = current['password']
                parsed['has_password'] = True
        payload = {
            'enabled': parsed['enabled'],
            'hostname': parsed['hostname'],
            'port': parsed['port'],
            'username': parsed['username'],
            'password': parsed['password'],
        }
        if set_setting_encrypted(SETTING_GEMINI_SOCKS5, json.dumps(payload, ensure_ascii=False)):
            updated.append(SETTING_GEMINI_SOCKS5)

    return get_ai_reply_settings(get_setting=get_setting, get_setting_decrypted=get_setting_decrypted)
