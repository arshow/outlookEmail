"""Shared constants for AI reply."""

from __future__ import annotations

CONTEXT_SCOPE_CURRENT = 'current'
CONTEXT_SCOPE_CONTACT_LOCAL = 'contact_local'
CONTEXT_SCOPES = (CONTEXT_SCOPE_CURRENT, CONTEXT_SCOPE_CONTACT_LOCAL)

PROVIDER_GEMINI = 'gemini'
PROVIDER_DEEPSEEK = 'deepseek'
PROVIDERS = (PROVIDER_GEMINI, PROVIDER_DEEPSEEK)

DEFAULT_PROVIDER = PROVIDER_GEMINI
DEFAULT_GEMINI_MODEL = 'gemini-2.5-flash'
DEFAULT_DEEPSEEK_MODEL = 'deepseek-chat'
DEFAULT_GEMINI_BASE_URL = 'https://generativelanguage.googleapis.com'
DEFAULT_DEEPSEEK_BASE_URL = 'https://api.deepseek.com'

# Hard limits for contact_local history (local-only; not user-configurable).
# Cap is a safety ceiling for prompt size; AI never fetches remote IMAP/Graph.
HISTORY_MAX_MESSAGES = 500
HISTORY_BODY_MAX_CHARS = 3000

RISK_LEVELS = ('green', 'yellow', 'red')
INTENTS = (
    'refund',
    'complaint',
    'shipping_status',
    'delivery_delay',
    'account_access',
    'verification',
    'billing',
    'general_question',
    'unknown',
)

REFINE_MODES = ('shorter', 'politer', 'regenerate', 'custom', 'translate_zh', 'translate')

SETTING_ENABLED = 'ai_reply_enabled'
SETTING_PROVIDER = 'ai_reply_provider'
SETTING_MODEL = 'ai_reply_model'
SETTING_GEMINI_API_KEY = 'ai_reply_gemini_api_key'
SETTING_DEEPSEEK_API_KEY = 'ai_reply_deepseek_api_key'
SETTING_GEMINI_BASE_URL = 'ai_reply_gemini_base_url'
SETTING_DEEPSEEK_BASE_URL = 'ai_reply_deepseek_base_url'
SETTING_GEMINI_SOCKS5 = 'ai_reply_gemini_socks5'
SETTING_SYSTEM_PERSONA = 'ai_reply_system_persona'

ANALYSIS_JSON_SCHEMA = {
    'type': 'object',
    'required': [
        'sourceLanguage',
        'summaryZh',
        'intent',
        'riskLevel',
        'riskReasons',
        'matchedRuleIds',
        'matchedKnowledgeIds',
        'missingFacts',
        'internalAdviceZh',
        'replyLanguage',
        'replyText',
        'replyTextZh',
        'confidence',
        'requiresHumanConfirmation',
    ],
    'properties': {
        'sourceLanguage': {'type': 'string'},
        'summaryZh': {'type': 'string'},
        'intent': {'type': 'string'},
        'riskLevel': {'type': 'string'},
        'riskReasons': {'type': 'array', 'items': {'type': 'string'}},
        'matchedRuleIds': {'type': 'array', 'items': {'type': 'string'}},
        'matchedKnowledgeIds': {'type': 'array', 'items': {'type': 'string'}},
        'missingFacts': {'type': 'array', 'items': {'type': 'string'}},
        'internalAdviceZh': {'type': 'string'},
        'replyLanguage': {'type': 'string'},
        'replyText': {'type': 'string'},
        'replyTextZh': {'type': 'string'},
        'confidence': {'type': 'number'},
        'requiresHumanConfirmation': {'type': 'boolean'},
    },
}

REFINED_REPLY_SCHEMA = {
    'type': 'object',
    'required': ['replyText', 'replyTextZh', 'replyLanguage'],
    'properties': {
        'replyText': {'type': 'string'},
        'replyTextZh': {'type': 'string'},
        'replyLanguage': {'type': 'string'},
    },
}

ANALYSIS_JSON_EXAMPLE = {
    'sourceLanguage': 'en',
    'summaryZh': '对方询问订单或问题进展。',
    'intent': 'general_question',
    'riskLevel': 'green',
    'riskReasons': [],
    'matchedRuleIds': [],
    'matchedKnowledgeIds': [],
    'missingFacts': [],
    'internalAdviceZh': '核对事实后再回复。',
    'replyLanguage': 'en',
    'replyText': 'Thanks for your message. I will check and get back to you shortly.',
    'replyTextZh': '感谢您的留言。我会核实后尽快回复您。',
    'confidence': 0.8,
    'requiresHumanConfirmation': False,
}
