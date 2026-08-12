"""AI smart reply helpers for Outlook email compose."""

from outlook_web.ai.service import analyze_email, refine_reply
from outlook_web.ai.settings import get_ai_reply_settings, save_ai_reply_settings

__all__ = [
    'analyze_email',
    'refine_reply',
    'get_ai_reply_settings',
    'save_ai_reply_settings',
]
