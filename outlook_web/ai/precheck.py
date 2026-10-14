"""Inbox precheck: skip Shopify notices, ask Clef if it is a real customer, then auto-analyze."""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Mapping, Optional

from outlook_web.ai.clef import call_clef, resolve_clef_runtime
from outlook_web.ai.constants import (
    CONTEXT_SCOPE_CURRENT,
    PRECHECK_BODY_MAX_CHARS,
    PRECHECK_CUSTOMER_QUESTIONS,
    PRECHECK_CUSTOMER_THRESHOLD,
    PRECHECK_DECISION_CUSTOMER_ANALYZED,
    PRECHECK_DECISION_FAILED,
    PRECHECK_DECISION_MISSING_CLEF,
    PRECHECK_DECISION_NOT_CUSTOMER,
    PRECHECK_DECISION_SKIPPED_NOTIFICATION,
    PRECHECK_FOLDER_ALIASES,
    PRECHECK_FOLDERS,
)
from outlook_web.ai.service import analyze_email
from outlook_web.mail_reply_address import is_shopify_platform_address, looks_like_shopify_contact_form


def email_sender(email_item: Mapping[str, Any]) -> str:
    return str(email_item.get('from') or email_item.get('sender') or '').strip()


def email_subject(email_item: Mapping[str, Any]) -> str:
    return str(email_item.get('subject') or '').strip()


def email_body_text(email_item: Mapping[str, Any]) -> str:
    body = str(email_item.get('body') or '').strip()
    preview = str(email_item.get('body_preview') or '').strip()
    return body or preview


def email_message_id(email_item: Mapping[str, Any]) -> str:
    return str(email_item.get('id') or email_item.get('message_id') or '').strip()


def normalize_precheck_folder(folder: Any) -> str:
    raw = str(folder or 'inbox').strip().lower()
    if raw in PRECHECK_FOLDER_ALIASES:
        return PRECHECK_FOLDER_ALIASES[raw]
    return raw


def is_precheck_folder(folder: Any) -> bool:
    return normalize_precheck_folder(folder) in PRECHECK_FOLDERS


def collect_new_inbox_emails(emails: Any, new_message_ids: Any) -> List[Dict[str, Any]]:
    wanted = set()
    for item in new_message_ids or []:
        if isinstance(item, Mapping):
            message_id = str(item.get('id') or item.get('message_id') or '').strip()
        else:
            message_id = str(item or '').strip()
        if message_id:
            wanted.add(message_id)
    matched: List[Dict[str, Any]] = []
    for email in emails or []:
        if not isinstance(email, Mapping):
            continue
        message_id = email_message_id(email)
        if message_id and message_id in wanted:
            matched.append(dict(email))
    return matched


def classify_fixed_notification(email_item: Mapping[str, Any]) -> Optional[str]:
    sender = email_sender(email_item)
    subject = email_subject(email_item)
    body = email_body_text(email_item)
    if not is_shopify_platform_address(sender):
        return None
    if looks_like_shopify_contact_form(sender, subject, body):
        return None
    return PRECHECK_DECISION_SKIPPED_NOTIFICATION


def extract_noul_probability(answer: Any) -> Optional[float]:
    if isinstance(answer, bool):
        return 1.0 if answer else 0.0
    if isinstance(answer, (int, float)):
        return float(answer)
    if not isinstance(answer, Mapping):
        return None
    for key in ('probability', 'noul', 'true', 'p', 'score', 'confidence'):
        value = answer.get(key)
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        if isinstance(value, (int, float)):
            return float(value)
    choice = str(answer.get('choice') or '').strip().lower()
    if choice in {'true', 'yes', '1'}:
        return 1.0
    if choice in {'false', 'no', '0'}:
        return 0.0
    return None


def build_clef_state(email_item: Mapping[str, Any]) -> Dict[str, str]:
    body = email_body_text(email_item)[:PRECHECK_BODY_MAX_CHARS]
    preview = str(email_item.get('body_preview') or '').strip()[:PRECHECK_BODY_MAX_CHARS]
    return {
        'from': email_sender(email_item),
        'subject': email_subject(email_item),
        'preview': preview,
        'body': body,
    }


def build_analysis_detail(email_item: Mapping[str, Any], message_id: str, folder: str = 'inbox') -> Dict[str, Any]:
    return {
        'id': message_id,
        'subject': email_subject(email_item) or '无主题',
        'from': email_sender(email_item) or '未知',
        'to': str(email_item.get('to') or email_item.get('recipients') or ''),
        'body': email_body_text(email_item),
        'body_preview': str(email_item.get('body_preview') or ''),
        'body_type': email_item.get('body_type') or 'text',
        'folder': normalize_precheck_folder(folder) or 'inbox',
        'id_mode': str(email_item.get('id_mode') or ''),
    }


def generation_ready(settings: Mapping[str, Any]) -> bool:
    if not settings.get('enabled'):
        return False
    provider = str(settings.get('provider') or '').strip().lower()
    if provider == 'deepseek':
        return bool(settings.get('deepseek_api_key'))
    return bool(settings.get('gemini_api_key'))


def try_begin_precheck_run(
    db,
    *,
    account_id: int,
    account_email: str,
    folder: str,
    message_id: str,
    id_mode: str,
    sender: str,
    subject: str,
) -> bool:
    cursor = db.execute(
        '''
        INSERT OR IGNORE INTO ai_precheck_runs (
            account_id, account_email, folder, message_id, id_mode,
            decision, sender, subject
        ) VALUES (?, ?, ?, ?, ?, '', ?, ?)
        ''',
        (account_id, account_email, folder, message_id, id_mode, sender, subject),
    )
    db.commit()
    return int(cursor.rowcount or 0) > 0


def finish_precheck_run(
    db,
    *,
    account_id: int,
    folder: str,
    message_id: str,
    decision: str,
    clef_answers: Any = None,
    analyze_run_id: Any = None,
    error_message: str = '',
) -> None:
    payload = None
    if clef_answers is not None:
        payload = json.dumps(clef_answers, ensure_ascii=False, default=str)
    db.execute(
        '''
        UPDATE ai_precheck_runs
        SET decision = ?,
            clef_answers = ?,
            analyze_run_id = ?,
            error_message = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE account_id = ? AND folder = ? AND message_id = ?
        ''',
        (
            decision,
            payload,
            int(analyze_run_id) if analyze_run_id else None,
            error_message or None,
            account_id,
            folder,
            message_id,
        ),
    )
    db.commit()


def process_inbox_precheck_item(
    *,
    db,
    settings: Dict[str, Any],
    account: Mapping[str, Any],
    email_item: Mapping[str, Any],
    folder: str = 'inbox',
    call_clef_fn: Callable[..., Dict[str, Any]] = call_clef,
    analyze_fn: Callable[..., Dict[str, Any]] = analyze_email,
) -> Dict[str, Any]:
    account_id = int(account.get('id') or 0)
    account_email = str(account.get('email') or '').strip()
    message_id = email_message_id(email_item)
    folder = normalize_precheck_folder(folder)
    if not account_id or not account_email or not message_id:
        return {'success': False, 'skipped': True, 'reason': 'invalid_item'}
    if folder not in PRECHECK_FOLDERS:
        return {'success': False, 'skipped': True, 'reason': 'unsupported_folder'}

    sender = email_sender(email_item)
    subject = email_subject(email_item)
    id_mode = str(email_item.get('id_mode') or '')
    if not try_begin_precheck_run(
        db,
        account_id=account_id,
        account_email=account_email,
        folder=folder,
        message_id=message_id,
        id_mode=id_mode,
        sender=sender,
        subject=subject,
    ):
        return {'success': True, 'skipped': True, 'reason': 'already_processed', 'message_id': message_id}

    try:
        fixed = classify_fixed_notification(email_item)
        if fixed == PRECHECK_DECISION_SKIPPED_NOTIFICATION:
            finish_precheck_run(
                db,
                account_id=account_id,
                folder=folder,
                message_id=message_id,
                decision=PRECHECK_DECISION_SKIPPED_NOTIFICATION,
            )
            return {
                'success': True,
                'decision': PRECHECK_DECISION_SKIPPED_NOTIFICATION,
                'message_id': message_id,
            }

        try:
            credentials = resolve_clef_runtime(settings)
        except ValueError:
            finish_precheck_run(
                db,
                account_id=account_id,
                folder=folder,
                message_id=message_id,
                decision=PRECHECK_DECISION_MISSING_CLEF,
                error_message='未配置 Cloudflare Clef',
            )
            return {
                'success': True,
                'decision': PRECHECK_DECISION_MISSING_CLEF,
                'message_id': message_id,
            }

        clef_result = call_clef_fn(
            account_id=credentials['account_id'],
            api_token=credentials['api_token'],
            model=credentials['model'],
            state=build_clef_state(email_item),
            questions=PRECHECK_CUSTOMER_QUESTIONS,
        )
        answers = clef_result.get('answers') if isinstance(clef_result, dict) else {}
        probability = extract_noul_probability((answers or {}).get('is_customer'))
        if probability is None:
            raise RuntimeError('Clef 未返回 is_customer 概率')
        if probability < PRECHECK_CUSTOMER_THRESHOLD:
            finish_precheck_run(
                db,
                account_id=account_id,
                folder=folder,
                message_id=message_id,
                decision=PRECHECK_DECISION_NOT_CUSTOMER,
                clef_answers=answers,
            )
            return {
                'success': True,
                'decision': PRECHECK_DECISION_NOT_CUSTOMER,
                'message_id': message_id,
                'probability': probability,
            }

        if not generation_ready(settings):
            finish_precheck_run(
                db,
                account_id=account_id,
                folder=folder,
                message_id=message_id,
                decision=PRECHECK_DECISION_FAILED,
                clef_answers=answers,
                error_message='AI 智能回复未启用或未配置生成模型',
            )
            return {
                'success': False,
                'decision': PRECHECK_DECISION_FAILED,
                'message_id': message_id,
            }

        analysis = analyze_fn(
            db=db,
            settings=settings,
            account_email=account_email,
            account_id=account_id,
            message_id=message_id,
            current_detail=build_analysis_detail(email_item, message_id, folder),
            context_scope=CONTEXT_SCOPE_CURRENT,
        )
        finish_precheck_run(
            db,
            account_id=account_id,
            folder=folder,
            message_id=message_id,
            decision=PRECHECK_DECISION_CUSTOMER_ANALYZED,
            clef_answers=answers,
            analyze_run_id=analysis.get('run_id') if isinstance(analysis, dict) else None,
        )
        return {
            'success': True,
            'decision': PRECHECK_DECISION_CUSTOMER_ANALYZED,
            'message_id': message_id,
            'run_id': analysis.get('run_id') if isinstance(analysis, dict) else None,
            'probability': probability,
        }
    except Exception as exc:
        finish_precheck_run(
            db,
            account_id=account_id,
            folder=folder,
            message_id=message_id,
            decision=PRECHECK_DECISION_FAILED,
            error_message=str(exc),
        )
        return {
            'success': False,
            'decision': PRECHECK_DECISION_FAILED,
            'message_id': message_id,
            'error': str(exc),
        }
