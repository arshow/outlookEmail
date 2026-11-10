"""单个邮箱批量导出：后台任务把邮件原文打成 ZIP。"""

from __future__ import annotations

import csv
import imaplib
import io
import json
import os
import re
import secrets
import threading
import zipfile
from datetime import datetime, timezone
from email import message_from_bytes
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from flask import jsonify, request, send_file


MAILBOX_EXPORT_MAX_MESSAGES = max(1, min(int(os.getenv('MAILBOX_EXPORT_MAX_MESSAGES', '2000')), 10000))
MAILBOX_EXPORT_MAX_CONCURRENT = 1
MAILBOX_EXPORT_JOB_TTL_SECONDS = 30 * 60
MAILBOX_EXPORT_STALE_FILE_SECONDS = 6 * 3600
MAILBOX_EXPORT_GRAPH_PAGE_SIZE = 50
MAILBOX_EXPORT_WELL_KNOWN_FOLDERS = ('inbox', 'junkemail', 'sentitems', 'drafts', 'archive', 'deleteditems')
MAILBOX_EXPORT_FOLDER_PRIORITY = {
    'inbox': 0,
    'junkemail': 1,
    'sentitems': 2,
    'drafts': 3,
    'archive': 4,
    'deleteditems': 5,
}
MAILBOX_EXPORT_FOLDER_LABELS = {
    'inbox': '收件箱',
    'junkemail': '垃圾邮件',
    'sentitems': '已发送',
    'drafts': '草稿',
    'archive': '存档',
    'deleteditems': '已删除邮件',
}
LOCAL_EXPORT_NOTE = '这些 .eml 由本地已保留的主题、发件人、收件人、时间和正文生成。'

_mailbox_export_lock = threading.Lock()
mailbox_export_jobs: Dict[str, Dict[str, Any]] = {}


class MailboxExportContext:
    def __init__(self):
        self.proxy_url = None
        self.fallback_proxy_urls = None
        self.graph_token = None
        self.graph_error = ''
        self.imap_conn = None
        self.imap_server = ''
        self.imap_error = ''
        self.imap_selected = None


def mailbox_export_error_text(error: Any, fallback: str = '导出失败') -> str:
    if isinstance(error, dict):
        message = str(error.get('message') or error.get('error') or '').strip()
        return message or fallback
    text = str(error or '').strip()
    return text or fallback


def sanitize_export_path_part(value: Any, fallback: str) -> str:
    text = sanitize_attachment_filename(str(value or ''), fallback)
    text = re.sub(r'[<>:"|?*\x00-\x1f]', '_', text)
    text = re.sub(r'\s+', ' ', text).strip(' .')
    if not text or text in {'.', '..'}:
        text = fallback
    return text[:80]


def unique_zip_name(name: str, used_names: set) -> str:
    candidate = name
    root, extension = os.path.splitext(name)
    counter = 2
    while candidate in used_names:
        candidate = f'{root} ({counter}){extension}'
        counter += 1
    used_names.add(candidate)
    return candidate


def export_message_date_prefix(*values: Any) -> str:
    for value in values:
        text = str(value or '').strip()
        if not text:
            continue
        iso_match = re.search(r'(\d{4})-(\d{2})-(\d{2})', text)
        if iso_match:
            return ''.join(iso_match.groups())
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            parsed = None
        if parsed is not None:
            return parsed.strftime('%Y%m%d')
    return ''


def build_export_download_name(email_addr: str) -> str:
    label = sanitize_export_path_part(email_addr, 'mailbox')
    return f'{label}-emails.zip'


def build_export_eml_filename(folder_label: str, index: int, subject: str, date_prefix: str, used_names: set) -> str:
    folder_part = sanitize_export_path_part(folder_label, 'mail')
    subject_part = sanitize_export_path_part(subject, 'message')
    date_part = f'{date_prefix}_' if date_prefix else ''
    name = f'{folder_part}/{date_part}{index:04d}_{subject_part}.eml'
    return unique_zip_name(name, used_names)


def mailbox_export_directory() -> str:
    database_dir = os.path.dirname(os.path.abspath(DATABASE))
    path = os.path.join(database_dir, 'mailbox-exports')
    os.makedirs(path, exist_ok=True)
    return path


def _remove_export_file(path: str) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def public_mailbox_export_job(job: Dict[str, Any]) -> Dict[str, Any]:
    status = str(job.get('status') or '')
    return {
        'job_id': job.get('job_id') or '',
        'status': status,
        'email': job.get('email') or '',
        'scope': job.get('scope') or '',
        'total': int(job.get('total') or 0),
        'completed': int(job.get('completed') or 0),
        'failed_count': int(job.get('failed_count') or 0),
        'current_folder': job.get('current_folder') or '',
        'message': job.get('message') or '',
        'error': job.get('error') or '',
        'truncated': bool(job.get('truncated')),
        'max_messages': int(job.get('max_messages') or MAILBOX_EXPORT_MAX_MESSAGES),
        'filename': job.get('filename') or '',
        'download_ready': status == 'succeeded' and bool(job.get('file_path')),
    }


def update_mailbox_export_job(job_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    with _mailbox_export_lock:
        job = mailbox_export_jobs.get(job_id)
        if not job:
            return None
        job.update(fields)
        return public_mailbox_export_job(job)


def mailbox_export_cancel_requested(job_id: str) -> bool:
    with _mailbox_export_lock:
        job = mailbox_export_jobs.get(job_id)
        if not job:
            return True
        return bool(job.get('cancel_requested')) or job.get('status') == 'cancelled'


def cleanup_mailbox_export_jobs(now: Optional[float] = None) -> None:
    current = time_module_now() if now is None else float(now)
    removed_paths: List[str] = []
    with _mailbox_export_lock:
        expired_ids = []
        for job_id, job in list(mailbox_export_jobs.items()):
            age = current - float(job.get('created_at') or current)
            status = job.get('status')
            if status in {'pending', 'running'} and age > MAILBOX_EXPORT_STALE_FILE_SECONDS:
                job['cancel_requested'] = True
            if status not in {'pending', 'running'} and age > MAILBOX_EXPORT_JOB_TTL_SECONDS:
                expired_ids.append(job_id)
        for job_id in expired_ids:
            job = mailbox_export_jobs.pop(job_id, None)
            if job and job.get('file_path'):
                removed_paths.append(job['file_path'])
        active_paths = {
            os.path.abspath(job['file_path'])
            for job in mailbox_export_jobs.values()
            if job.get('file_path')
        }
    for path in removed_paths:
        _remove_export_file(path)

    directory = mailbox_export_directory()
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        path = os.path.join(directory, name)
        if os.path.abspath(path) in active_paths or not os.path.isfile(path):
            continue
        try:
            if current - os.path.getmtime(path) > MAILBOX_EXPORT_STALE_FILE_SECONDS:
                os.remove(path)
        except OSError:
            continue


def time_module_now() -> float:
    return datetime.now(timezone.utc).timestamp()


def reset_mailbox_export_state() -> None:
    with _mailbox_export_lock:
        jobs = list(mailbox_export_jobs.values())
        mailbox_export_jobs.clear()
    for job in jobs:
        _remove_export_file(str(job.get('file_path') or ''))


def _well_known_target(folder: str, channel: str = '') -> Dict[str, Any]:
    return {
        'label': MAILBOX_EXPORT_FOLDER_LABELS.get(folder, folder),
        'folder': folder,
        'folder_id': '',
        'mailbox': '',
        'channel': channel,
        'well_known': folder,
    }


def _fallback_mailbox_targets() -> List[Dict[str, Any]]:
    return [
        _well_known_target('inbox'),
        _well_known_target('junkemail'),
        _well_known_target('deleteditems'),
    ]


def export_targets_from_folder_nodes(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    targets: List[Dict[str, Any]] = []
    seen = set()
    for node in nodes or []:
        if not isinstance(node, dict) or node.get('selectable') is False:
            continue
        well_known = str(node.get('well_known') or '').strip().lower()
        label = str(node.get('display_name') or node.get('name') or well_known or '文件夹').strip() or '文件夹'
        provider = str(node.get('provider') or '').strip().lower()
        folder_id = str(node.get('folder_id') or '').strip()
        mailbox = str(node.get('mailbox') or '').strip()
        node_id = str(node.get('id') or '').strip()
        if provider == 'graph' or (folder_id and provider != 'imap'):
            target = {
                'label': label,
                'folder': well_known if well_known in MAILBOX_EXPORT_WELL_KNOWN_FOLDERS else '',
                'folder_id': folder_id or node_id,
                'mailbox': '',
                'channel': 'graph',
                'well_known': well_known,
            }
        else:
            target = {
                'label': label,
                'folder': well_known if well_known in MAILBOX_EXPORT_WELL_KNOWN_FOLDERS else 'inbox',
                'folder_id': '',
                'mailbox': mailbox or node_id,
                'channel': 'imap',
                'well_known': well_known,
            }
        if not target['folder_id'] and not target['mailbox'] and not target['folder']:
            continue
        identity = (
            target['channel'],
            target['folder_id'],
            target['mailbox'],
            target['folder'],
        )
        if identity in seen:
            continue
        seen.add(identity)
        targets.append(target)
    targets.sort(key=lambda item: (
        MAILBOX_EXPORT_FOLDER_PRIORITY.get(str(item.get('well_known') or ''), 50),
        str(item.get('label') or '').lower(),
    ))
    return targets


def build_mailbox_export_targets(account: Dict[str, Any], scope: str, folder: str = '',
                                 folder_id: str = '', mailbox: str = '') -> Dict[str, Any]:
    scope_name = str(scope or 'mailbox').strip().lower()
    explicit_folder_id = str(folder_id or '').strip()
    explicit_mailbox = str(mailbox or '').strip()
    raw_folder = str(folder or '').strip()
    if raw_folder.lower().startswith('graph:') and not explicit_folder_id:
        explicit_folder_id = raw_folder.split(':', 1)[1].strip()
        raw_folder = ''
    elif raw_folder.lower().startswith('imap:') and not explicit_mailbox:
        explicit_mailbox = raw_folder.split(':', 1)[1].strip()
        raw_folder = ''

    if explicit_folder_id and explicit_mailbox:
        return {'success': False, 'error': 'folder_id 与 mailbox 不能同时使用', 'targets': []}

    if scope_name == 'folder':
        if explicit_folder_id:
            return {
                'success': True,
                'targets': [{
                    'label': '文件夹',
                    'folder': '',
                    'folder_id': explicit_folder_id,
                    'mailbox': '',
                    'channel': 'graph',
                    'well_known': '',
                }],
            }
        if explicit_mailbox:
            return {
                'success': True,
                'targets': [{
                    'label': explicit_mailbox,
                    'folder': 'inbox',
                    'folder_id': '',
                    'mailbox': explicit_mailbox,
                    'channel': 'imap',
                    'well_known': '',
                }],
            }
        folder_name = normalize_folder_name(raw_folder or 'all')
        if folder_name == 'all':
            return {
                'success': True,
                'targets': [_well_known_target('inbox'), _well_known_target('junkemail')],
            }
        if folder_name not in VALID_MAIL_FOLDERS:
            return {
                'success': False,
                'error': f'folder 参数无效，支持: {", ".join(sorted(VALID_MAIL_FOLDERS))}',
                'targets': [],
            }
        return {'success': True, 'targets': [_well_known_target(folder_name)]}

    listed = list_account_mail_folders(account)
    if listed.get('success'):
        targets = export_targets_from_folder_nodes(listed.get('folders') or [])
        if targets:
            return {'success': True, 'targets': targets}
    return {'success': True, 'targets': _fallback_mailbox_targets(), 'folder_list_fallback': True}


def export_target_channel(account: Dict[str, Any], target: Dict[str, Any]) -> str:
    if str(account.get('account_type') or '').strip().lower() == 'imap':
        return 'imap'
    channel = str(target.get('channel') or '').strip().lower()
    if channel in {'graph', 'imap'}:
        return channel
    if str(target.get('folder_id') or '').strip():
        return 'graph'
    if str(target.get('mailbox') or '').strip():
        return 'imap'
    order = get_outlook_mail_channel_order(account)
    return order[0] if order else 'graph'


def _proxy_for_context(account: Dict[str, Any], ctx: MailboxExportContext) -> None:
    if ctx.proxy_url is None:
        ctx.proxy_url = get_account_proxy_url(account) or ''
    if ctx.fallback_proxy_urls is None:
        ctx.fallback_proxy_urls = get_account_proxy_failover_urls(account) or []


def ensure_graph_token(account: Dict[str, Any], ctx: MailboxExportContext, force: bool = False) -> str:
    if ctx.graph_token and not force:
        return ctx.graph_token
    _proxy_for_context(account, ctx)
    token_result = get_access_token_graph_result(
        account.get('client_id'),
        account.get('refresh_token'),
        ctx.proxy_url,
        ctx.fallback_proxy_urls,
    )
    if not token_result.get('success'):
        ctx.graph_error = mailbox_export_error_text(token_result.get('error'), '获取 Graph 令牌失败')
        ctx.graph_token = None
        return ''
    ctx.graph_token = str(token_result.get('access_token') or '')
    ctx.graph_error = ''
    return ctx.graph_token


def graph_export_http_error(response, action: str) -> str:
    details = ''
    try:
        payload = response.json() if getattr(response, 'content', None) else {}
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        error = payload.get('error')
        if isinstance(error, dict):
            details = str(error.get('message') or error.get('code') or '')
        elif error:
            details = str(error)
    details = sanitize_error_details(details)[:180] if details else ''
    status_code = getattr(response, 'status_code', 0)
    suffix = f'：{details}' if details else ''
    return f'{action}失败（HTTP {status_code}）{suffix}'


def list_graph_export_refs(account: Dict[str, Any], target: Dict[str, Any], ctx: MailboxExportContext,
                           limit: int, should_cancel: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    token = ensure_graph_token(account, ctx)
    if not token:
        return {'success': False, 'error': ctx.graph_error or '获取 Graph 令牌失败', 'refs': []}

    segment = quote(str(target.get('folder_id') or target.get('folder') or 'inbox').strip(), safe='')
    url = f'https://graph.microsoft.com/v1.0/me/mailFolders/{segment}/messages'
    params: Optional[Dict[str, str]] = {
        '$top': str(min(MAILBOX_EXPORT_GRAPH_PAGE_SIZE, max(1, limit))),
        '$select': 'id,subject,receivedDateTime,from',
        '$orderby': 'receivedDateTime desc',
    }
    refs: List[Dict[str, Any]] = []
    seen_urls = set()
    retried_without_order = False
    token_retried = False
    hit_cap = False

    while url and len(refs) < limit:
        if should_cancel and should_cancel():
            return {'success': False, 'cancelled': True, 'error': '导出已取消', 'refs': []}
        if url in seen_urls:
            break
        request_params = params
        request_kwargs = {
            'headers': {'Authorization': f'Bearer {token}'},
            'timeout': max(HTTP_REQUEST_TIMEOUT, 60),
            'proxy_url': ctx.proxy_url,
            'fallback_proxy_urls': ctx.fallback_proxy_urls,
        }
        if request_params:
            request_kwargs['params'] = request_params
        response = get_with_proxy_fallback(url, **request_kwargs)
        if getattr(response, 'status_code', 0) == 401 and not token_retried:
            token_retried = True
            token = ensure_graph_token(account, ctx, force=True)
            if not token:
                return {'success': False, 'error': ctx.graph_error or 'Graph 令牌已失效', 'refs': []}
            continue
        if (
            getattr(response, 'status_code', 0) == 400
            and request_params
            and '$orderby' in request_params
            and not retried_without_order
        ):
            retried_without_order = True
            params = dict(request_params)
            params.pop('$orderby', None)
            continue
        if getattr(response, 'status_code', 0) != 200:
            return {'success': False, 'error': graph_export_http_error(response, '读取邮件列表'), 'refs': []}
        seen_urls.add(url)
        try:
            payload = response.json() or {}
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        for item in payload.get('value') or []:
            if len(refs) >= limit:
                hit_cap = True
                break
            if not isinstance(item, dict):
                continue
            message_id = str(item.get('id') or '').strip()
            if not message_id:
                continue
            sender = ''
            from_block = item.get('from') if isinstance(item.get('from'), dict) else {}
            email_address = from_block.get('emailAddress') if isinstance(from_block, dict) else {}
            if isinstance(email_address, dict):
                sender = str(email_address.get('address') or email_address.get('name') or '')
            refs.append({
                'target': target,
                'id': message_id,
                'id_mode': 'graph',
                'channel': 'graph',
                'subject': str(item.get('subject') or ''),
                'from': sender,
                'date': str(item.get('receivedDateTime') or ''),
            })
        next_url = str(payload.get('@odata.nextLink') or '').strip()
        if len(refs) >= limit:
            if hit_cap or next_url:
                hit_cap = True
            break
        if not next_url:
            break
        url = next_url
        params = None

    return {'success': True, 'refs': refs, 'truncated': bool(hit_cap), 'error': ''}


def close_export_imap(ctx: MailboxExportContext) -> None:
    connection = ctx.imap_conn
    ctx.imap_conn = None
    ctx.imap_selected = None
    if connection is None:
        return
    try:
        connection.logout()
    except Exception:
        try:
            connection.shutdown()
        except Exception:
            pass


def ensure_export_imap(account: Dict[str, Any], ctx: MailboxExportContext):
    if ctx.imap_conn is not None:
        try:
            ctx.imap_conn.noop()
            return ctx.imap_conn
        except Exception:
            close_export_imap(ctx)

    _proxy_for_context(account, ctx)
    if str(account.get('account_type') or '').strip().lower() == 'imap':
        try:
            connection = create_imap_connection(
                account.get('imap_host', ''),
                account.get('imap_port', 993),
                ctx.proxy_url or '',
            )
            connection.login(account.get('email', ''), account.get('imap_password', ''))
        except imaplib.IMAP4.error as exc:
            ctx.imap_error = normalize_imap_auth_error(
                account.get('provider', 'custom'),
                account.get('imap_host', ''),
                str(exc),
            )
            return None
        except Exception as exc:
            ctx.imap_error = sanitize_error_details(str(exc))[:200] or 'IMAP 连接失败'
            return None
        send_imap_id(connection, account.get('provider', 'custom'), account.get('imap_host', ''))
        ctx.imap_conn = connection
        ctx.imap_error = ''
        ctx.imap_selected = None
        return connection

    token_result = get_access_token_imap_result(
        account.get('client_id'),
        account.get('refresh_token'),
        ctx.proxy_url,
        ctx.fallback_proxy_urls,
    )
    if not token_result.get('success'):
        ctx.imap_error = mailbox_export_error_text(token_result.get('error'), '获取 IMAP 令牌失败')
        return None
    access_token = token_result.get('access_token') or ''
    auth_string = f"user={account.get('email')}\1auth=Bearer {access_token}\1\1".encode('utf-8')
    servers: List[str] = []
    if ctx.imap_server:
        servers.append(ctx.imap_server)
    for server in (IMAP_SERVER_NEW, IMAP_SERVER_OLD):
        if server not in servers:
            servers.append(server)
    last_error = 'IMAP 连接失败'
    for server in servers:
        connection = None
        try:
            with proxy_socket_context(ctx.proxy_url):
                connection = imaplib.IMAP4_SSL(server, IMAP_PORT, timeout=IMAP_TIMEOUT)
            connection.authenticate('XOAUTH2', lambda _unused, token=auth_string: token)
            ctx.imap_conn = connection
            ctx.imap_server = server
            ctx.imap_error = ''
            ctx.imap_selected = None
            return connection
        except Exception as exc:
            last_error = sanitize_error_details(str(exc))[:200] or last_error
            if connection is not None:
                try:
                    connection.logout()
                except Exception:
                    pass
    ctx.imap_error = last_error
    return None


def select_export_imap_folder(account: Dict[str, Any], target: Dict[str, Any], ctx: MailboxExportContext) -> Dict[str, Any]:
    connection = ensure_export_imap(account, ctx)
    if connection is None:
        return {'success': False, 'error': ctx.imap_error or 'IMAP 连接失败'}
    select_key = (str(target.get('mailbox') or ''), str(target.get('folder') or 'inbox'))
    if ctx.imap_selected == select_key:
        return {'success': True, 'connection': connection}
    provider = account.get('provider') or ('custom' if account.get('account_type') == 'imap' else 'outlook')
    try:
        selected, diagnostics = resolve_imap_folder(
            connection,
            provider,
            target.get('folder') or 'inbox',
            readonly=True,
            mailbox=target.get('mailbox') or None,
        )
    except Exception as exc:
        close_export_imap(ctx)
        return {'success': False, 'error': sanitize_error_details(str(exc))[:200] or '打开文件夹失败'}
    if not selected:
        return {'success': False, 'error': '无法打开文件夹', 'diagnostics': diagnostics}
    ctx.imap_selected = select_key
    return {'success': True, 'connection': connection, 'diagnostics': diagnostics}


def list_imap_export_refs(account: Dict[str, Any], target: Dict[str, Any], ctx: MailboxExportContext,
                          limit: int, should_cancel: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    if should_cancel and should_cancel():
        return {'success': False, 'cancelled': True, 'error': '导出已取消', 'refs': []}
    selected = select_export_imap_folder(account, target, ctx)
    if not selected.get('success'):
        return {'success': False, 'error': selected.get('error') or '无法打开文件夹', 'refs': []}
    connection = selected['connection']
    diagnostics = selected.get('diagnostics') or {}
    try:
        message_ids, search_mode, search_attempts = search_imap_message_ids(connection)
    except Exception as exc:
        close_export_imap(ctx)
        return {'success': False, 'error': sanitize_error_details(str(exc))[:200] or '搜索邮件失败', 'refs': []}

    selected_exists = selected_imap_exists_count(diagnostics)
    truncated = False
    if message_ids is None and selected_exists > 0 and is_imap_search_response_too_large(search_attempts):
        close_export_imap(ctx)
        selected = select_export_imap_folder(account, target, ctx)
        if not selected.get('success'):
            return {'success': False, 'error': selected.get('error') or '搜索邮件失败', 'refs': []}
        connection = selected['connection']
        diagnostics = selected.get('diagnostics') or {}
        selected_exists = selected_imap_exists_count(diagnostics)
        message_ids = build_sequence_message_ids(selected_exists) if selected_exists > 0 else None
        search_mode = 'sequence'
    elif message_ids is None and selected_exists > 0 and is_imap_search_protocol_rejection(search_attempts):
        message_ids = build_sequence_message_ids(selected_exists)
        search_mode = 'sequence'
    elif not message_ids and selected_exists > 0:
        message_ids = build_sequence_message_ids(selected_exists)
        search_mode = 'sequence'
    if message_ids is None:
        return {'success': False, 'error': '搜索邮件失败', 'refs': []}

    ordered = list(reversed(message_ids))
    if len(ordered) > limit:
        ordered = ordered[:limit]
        truncated = True
    refs = []
    try:
        uidvalidity = get_imap_uidvalidity(connection)
        for message_id in ordered:
            text = message_id.decode('utf-8', errors='ignore') if isinstance(message_id, (bytes, bytearray)) else str(message_id)
            text = text.strip()
            if not text:
                continue
            if search_mode == 'sequence':
                status, data = connection.fetch(text, '(UID)')
                uids = imap_response_uids(data)
                if status != 'OK' or len(uids) != 1:
                    raise ImapMessageIdentityError('服务器未返回邮件 UID，无法安全导出')
                public_id = encode_imap_message_id(next(iter(uids)), uidvalidity)
            else:
                if not re.fullmatch(r'[1-9]\d*', text):
                    continue
                public_id = encode_imap_message_id(text, uidvalidity)
            refs.append({
                'target': target,
                'id': public_id,
                'id_mode': 'uid',
                'channel': 'imap',
                'subject': '',
                'from': '',
                'date': '',
            })
    except ImapMessageIdentityError as exc:
        return {'success': False, 'error': str(exc), 'refs': []}
    return {'success': True, 'refs': refs, 'truncated': truncated, 'error': ''}


def list_target_export_refs(account: Dict[str, Any], target: Dict[str, Any], ctx: MailboxExportContext,
                            limit: int, should_cancel: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    channel = export_target_channel(account, target)
    if channel == 'graph':
        graph_result = list_graph_export_refs(account, target, ctx, limit, should_cancel)
        if graph_result.get('success') or graph_result.get('cancelled') or target.get('folder_id'):
            return graph_result
        imap_result = list_imap_export_refs(account, target, ctx, limit, should_cancel)
        if imap_result.get('success') or imap_result.get('cancelled'):
            return imap_result
        graph_error = graph_result.get('error') or 'Graph 读取失败'
        imap_error = imap_result.get('error') or ''
        if imap_error and imap_error not in graph_error:
            graph_result['error'] = f'{graph_error}；IMAP：{imap_error}'
        return graph_result
    return list_imap_export_refs(account, target, ctx, limit, should_cancel)


def collect_mailbox_export_refs(account: Dict[str, Any], targets: List[Dict[str, Any]], max_messages: int,
                                ctx: MailboxExportContext, should_cancel: Optional[Callable[[], bool]] = None,
                                on_status: Optional[Callable[..., None]] = None) -> Dict[str, Any]:
    refs: List[Dict[str, Any]] = []
    folder_errors: List[Dict[str, str]] = []
    truncated = False
    limit = max(1, int(max_messages or MAILBOX_EXPORT_MAX_MESSAGES))

    for target in targets or []:
        if should_cancel and should_cancel():
            return {
                'success': False,
                'cancelled': True,
                'error': '导出已取消',
                'refs': [],
                'folder_errors': folder_errors,
                'truncated': truncated,
            }
        remaining = limit - len(refs)
        if remaining <= 0:
            truncated = True
            break
        label = str(target.get('label') or '文件夹')
        if on_status:
            on_status(f'正在读取{label}', total=len(refs), folder=label)
        result = list_target_export_refs(account, target, ctx, remaining, should_cancel)
        if result.get('cancelled'):
            return {
                'success': False,
                'cancelled': True,
                'error': '导出已取消',
                'refs': [],
                'folder_errors': folder_errors,
                'truncated': truncated,
            }
        if not result.get('success'):
            folder_errors.append({
                'folder': label,
                'error': mailbox_export_error_text(result.get('error'), '读取文件夹失败'),
            })
            continue
        refs.extend(result.get('refs') or [])
        if on_status:
            on_status(f'已找到 {len(refs)} 封', total=len(refs), folder=label)
        if result.get('truncated'):
            truncated = True
            break

    if not refs:
        error = folder_errors[0]['error'] if folder_errors else '没有可导出的邮件'
        return {
            'success': False,
            'cancelled': False,
            'error': error,
            'refs': [],
            'folder_errors': folder_errors,
            'truncated': truncated,
        }
    return {
        'success': True,
        'cancelled': False,
        'error': '',
        'refs': refs,
        'folder_errors': folder_errors,
        'truncated': truncated,
    }


def fetch_graph_export_raw(account: Dict[str, Any], ref: Dict[str, Any], ctx: MailboxExportContext) -> Dict[str, Any]:
    token = ensure_graph_token(account, ctx)
    if not token:
        return {'success': False, 'error': ctx.graph_error or '获取 Graph 令牌失败'}
    message_id = quote(str(ref.get('id') or '').strip(), safe='')
    url = f'https://graph.microsoft.com/v1.0/me/messages/{message_id}/$value'
    token_retried = False
    while True:
        response = get_with_proxy_fallback(
            url,
            headers={'Authorization': f'Bearer {token}'},
            timeout=max(HTTP_REQUEST_TIMEOUT, 60),
            proxy_url=ctx.proxy_url,
            fallback_proxy_urls=ctx.fallback_proxy_urls,
        )
        if getattr(response, 'status_code', 0) == 401 and not token_retried:
            token_retried = True
            token = ensure_graph_token(account, ctx, force=True)
            if not token:
                return {'success': False, 'error': ctx.graph_error or 'Graph 令牌已失效'}
            continue
        if getattr(response, 'status_code', 0) != 200:
            return {'success': False, 'error': graph_export_http_error(response, '读取邮件原文')}
        content = getattr(response, 'content', b'') or b''
        if isinstance(content, str):
            content = content.encode('utf-8')
        if not content:
            return {'success': False, 'error': '邮件原文为空'}
        return {'success': True, 'raw': bytes(content)}


def fetch_imap_export_raw(account: Dict[str, Any], ref: Dict[str, Any], ctx: MailboxExportContext) -> Dict[str, Any]:
    target = ref.get('target') or {}
    last_error = '读取邮件原文失败'
    for _attempt in (1, 2):
        selected = select_export_imap_folder(account, target, ctx)
        if not selected.get('success'):
            return {'success': False, 'error': selected.get('error') or '无法打开文件夹'}
        connection = selected['connection']
        try:
            raw = None
            for query in ('(BODY.PEEK[])', '(RFC822)'):
                status, data, _mode, _attempts = fetch_imap_message(
                    connection,
                    str(ref.get('id') or ''),
                    query,
                    preferred_mode=str(ref.get('id_mode') or 'uid'),
                )
                if status != 'OK':
                    continue
                parsed, _response_text = parse_imap_fetch_response(data)
                if parsed:
                    raw = parsed
                    break
            if raw:
                return {'success': True, 'raw': raw}
            return {'success': False, 'error': '读取邮件原文失败'}
        except Exception as exc:
            last_error = sanitize_error_details(str(exc))[:200] or last_error
            close_export_imap(ctx)
    return {'success': False, 'error': last_error}


def fetch_export_message_raw(account: Dict[str, Any], ref: Dict[str, Any], ctx: MailboxExportContext) -> Dict[str, Any]:
    channel = str(ref.get('channel') or export_target_channel(account, ref.get('target') or {})).strip().lower()
    if channel == 'graph':
        return fetch_graph_export_raw(account, ref, ctx)
    return fetch_imap_export_raw(account, ref, ctx)


def summarize_export_raw(raw: bytes) -> Dict[str, str]:
    try:
        message = message_from_bytes(raw)
    except Exception:
        return {'subject': '', 'from': '', 'to': '', 'date': '', 'message_id': ''}
    return {
        'subject': decode_header_value(message.get('Subject', '')) or '',
        'from': decode_header_value(message.get('From', '')) or '',
        'to': decode_header_value(message.get('To', '')) or '',
        'date': str(message.get('Date') or ''),
        'message_id': str(message.get('Message-ID') or ''),
    }


def build_mailbox_export_zip(path: str, account_email: str, refs: List[Dict[str, Any]],
                             fetch_raw: Callable[[Dict[str, Any]], Dict[str, Any]],
                             should_cancel: Optional[Callable[[], bool]] = None,
                             on_progress: Optional[Callable[..., None]] = None,
                             truncated: bool = False, folder_errors: Optional[List[Dict[str, str]]] = None,
                             max_messages: int = MAILBOX_EXPORT_MAX_MESSAGES, scope: str = 'mailbox',
                             folder_list_fallback: bool = False, note: str = '', source: str = '') -> Dict[str, Any]:
    used_names = set()
    index_rows = []
    errors: List[Dict[str, str]] = []
    exported_count = 0
    cancelled = False
    total = len(refs or [])

    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for index, ref in enumerate(refs or [], start=1):
            if should_cancel and should_cancel():
                cancelled = True
                break
            target = ref.get('target') or {}
            label = str(target.get('label') or '邮件')
            if on_progress:
                on_progress(exported_count, total, label)
            fetched = fetch_raw(ref) or {}
            if not fetched.get('success'):
                errors.append({
                    'folder': label,
                    'id': str(ref.get('id') or ''),
                    'error': mailbox_export_error_text(fetched.get('error'), '读取邮件原文失败'),
                })
                continue
            raw = fetched.get('raw') or b''
            if isinstance(raw, str):
                raw = raw.encode('utf-8')
            raw = bytes(raw)
            summary = summarize_export_raw(raw)
            subject = summary.get('subject') or str(ref.get('subject') or '') or '无主题'
            date_prefix = export_message_date_prefix(ref.get('date'), summary.get('date'))
            filename = build_export_eml_filename(label, index, subject, date_prefix, used_names)
            archive.writestr(filename, raw)
            exported_count += 1
            index_rows.append({
                'folder': label,
                'filename': filename,
                'subject': subject,
                'from': summary.get('from') or str(ref.get('from') or ''),
                'to': summary.get('to') or '',
                'date': summary.get('date') or str(ref.get('date') or ''),
                'message_id': summary.get('message_id') or '',
                'provider_id': str(ref.get('id') or ''),
            })
            if on_progress:
                on_progress(exported_count, total, label)

        if cancelled:
            return {
                'success': False,
                'cancelled': True,
                'exported_count': exported_count,
                'failed_count': len(errors),
                'errors': errors,
                'truncated': truncated,
            }

        manifest = {
            'email': account_email,
            'scope': scope,
            'exported_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            'exported_count': exported_count,
            'failed_count': len(errors),
            'truncated': bool(truncated),
            'max_messages': int(max_messages or MAILBOX_EXPORT_MAX_MESSAGES),
            'folder_errors': folder_errors or [],
            'errors': errors[:100],
            'folder_list_fallback': bool(folder_list_fallback),
            'source': source or ('local' if note else ''),
            'note': note or (
                '每个 .eml 是一封邮件的原文，包含完整邮件头。'
                + (' 未能读取完整文件夹列表，本次只导出了收件箱、垃圾邮件和已删除邮件。' if folder_list_fallback else '')
            ),
        }
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))

        csv_buffer = io.StringIO()
        writer = csv.DictWriter(csv_buffer, fieldnames=[
            '文件夹', '文件名', '主题', '发件人', '收件人', '时间', 'Message-ID',
        ])
        writer.writeheader()
        for row in index_rows:
            writer.writerow({
                '文件夹': row['folder'],
                '文件名': row['filename'],
                '主题': row['subject'],
                '发件人': row['from'],
                '收件人': row['to'],
                '时间': row['date'],
                'Message-ID': row['message_id'],
            })
        archive.writestr('index.csv', csv_buffer.getvalue().encode('utf-8-sig'))

        if errors or folder_errors:
            lines = []
            for item in folder_errors or []:
                lines.append(f"{item.get('folder') or '文件夹'}\t{item.get('error') or '读取失败'}")
            for item in errors:
                lines.append(f"{item.get('folder') or '邮件'}\t{item.get('id') or ''}\t{item.get('error') or '读取失败'}")
            archive.writestr('errors.txt', ('\n'.join(lines) + '\n').encode('utf-8'))

    return {
        'success': exported_count > 0,
        'cancelled': False,
        'exported_count': exported_count,
        'failed_count': len(errors),
        'errors': errors,
        'truncated': bool(truncated),
        'error': '' if exported_count > 0 else '没有成功导出的邮件',
    }


def _finish_mailbox_export_job(job_id: str, ctx: MailboxExportContext, **fields: Any) -> None:
    close_export_imap(ctx)
    file_path = ''
    with _mailbox_export_lock:
        job = mailbox_export_jobs.get(job_id)
        if not job:
            return
        file_path = str(job.get('file_path') or '')
        job.update(fields)
        if fields.get('status') != 'succeeded':
            job['file_path'] = ''
    if fields.get('status') != 'succeeded':
        _remove_export_file(file_path)


def run_mailbox_export_job(job_id: str) -> None:
    ctx = MailboxExportContext()
    try:
        with app.app_context():
            _run_mailbox_export_job(job_id, ctx)
    except Exception as exc:
        try:
            app.logger.exception('mailbox export failed for %s', job_id)
        except Exception:
            pass
        _finish_mailbox_export_job(
            job_id,
            ctx,
            status='failed',
            error=sanitize_error_details(str(exc))[:200] or '导出失败',
            message='导出失败',
        )


def export_folder_label(folder: str) -> str:
    normalized = str(folder or '').strip().lower()
    if normalized in MAILBOX_EXPORT_FOLDER_LABELS:
        return MAILBOX_EXPORT_FOLDER_LABELS[normalized]
    text = str(folder or '').strip()
    return text or '邮件'


def resolve_local_export_folders(scope: str, folder: str = '', folder_id: str = '',
                                 mailbox: str = '') -> Dict[str, Any]:
    """返回要导出的本地文件夹。folders 为 None 时表示该账号的全部已保留邮件。"""
    scope_name = str(scope or 'mailbox').strip().lower()
    explicit_folder_id = str(folder_id or '').strip()
    explicit_mailbox = str(mailbox or '').strip()
    raw_folder = str(folder or '').strip()
    if raw_folder.lower().startswith('graph:') and not explicit_folder_id:
        explicit_folder_id = raw_folder.split(':', 1)[1].strip()
        raw_folder = ''
    elif raw_folder.lower().startswith('imap:') and not explicit_mailbox:
        explicit_mailbox = raw_folder.split(':', 1)[1].strip()
        raw_folder = ''
    if explicit_folder_id and explicit_mailbox:
        return {'success': False, 'error': 'folder_id 与 mailbox 不能同时使用', 'folders': []}
    if scope_name != 'folder':
        return {'success': True, 'folders': None}
    if explicit_folder_id:
        return {'success': True, 'folders': [build_graph_folder_storage_key(explicit_folder_id)]}
    if explicit_mailbox:
        return {'success': True, 'folders': [build_imap_folder_storage_key(explicit_mailbox)]}
    folder_name = normalize_folder_name(raw_folder or 'all')
    if folder_name == 'all':
        return {'success': True, 'folders': ['inbox', 'junkemail']}
    if folder_name not in VALID_MAIL_FOLDERS and not is_custom_mail_folder_storage_key(folder_name):
        return {
            'success': False,
            'error': f'folder 参数无效，支持: {", ".join(sorted(VALID_MAIL_FOLDERS))}',
            'folders': [],
        }
    return {'success': True, 'folders': [folder_name]}


def build_retained_export_eml(row: Dict[str, Any]) -> bytes:
    message = EmailMessage()
    message['Subject'] = str(row.get('subject') or '无主题')
    message['From'] = str(row.get('sender') or '')
    message['To'] = str(row.get('recipients') or '')
    cc = str(row.get('cc') or '').strip()
    if cc:
        message['Cc'] = cc
    received_at = str(row.get('received_at') or '').strip()
    if received_at:
        message['Date'] = received_at
    internet_message_id = str(row.get('internet_message_id') or '').strip()
    if internet_message_id:
        message['Message-ID'] = internet_message_id

    body = str(row.get('body') or '')
    preview = str(row.get('body_preview') or '')
    body_type = str(row.get('body_type') or 'text').strip().lower()
    has_body = bool(int(row.get('body_cached') or 0)) and bool(body.strip())
    attachment_note = '这封邮件带有附件。' if int(row.get('has_attachments') or 0) else ''
    if has_body and 'html' in body_type:
        plain = preview.strip() or '这封邮件包含 HTML 正文。'
        if attachment_note:
            plain = f'{attachment_note}\n{plain}'
        message.set_content(plain)
        message.add_alternative(body, subtype='html')
    else:
        text = body.strip() if has_body else preview.strip()
        if attachment_note:
            text = f'{attachment_note}\n{text}'.strip()
        message.set_content(text)
    return message.as_bytes()


def _retained_export_row_rank(row: Dict[str, Any]) -> tuple:
    return (
        1 if int(row.get('body_cached') or 0) else 0,
        int(row.get('id') or 0),
    )


def load_local_mailbox_export_refs(account: Dict[str, Any], scope: str, folder: str = '',
                                   folder_id: str = '', mailbox: str = '',
                                   should_cancel: Optional[Callable[[], bool]] = None,
                                   on_status: Optional[Callable[..., None]] = None) -> Dict[str, Any]:
    resolved = resolve_local_export_folders(scope, folder, folder_id, mailbox)
    if not resolved.get('success'):
        return {
            'success': False,
            'cancelled': False,
            'error': resolved.get('error') or '文件夹参数无效',
            'refs': [],
            'folder_errors': [],
            'truncated': False,
        }
    if should_cancel and should_cancel():
        return {
            'success': False,
            'cancelled': True,
            'error': '导出已取消',
            'refs': [],
            'folder_errors': [],
            'truncated': False,
        }
    if on_status:
        on_status('正在读取本地邮件', total=0, folder='')

    account_id = int(account.get('id') or 0)
    folders = resolved.get('folders')
    sql = '''
        SELECT id, folder, provider_message_id, id_mode, subject, sender, sender_email,
               recipients, cc, received_at, received_at_sort, internet_message_id,
               is_read, is_flagged, has_attachments, body_preview, body, body_type, body_cached
        FROM retained_normal_mail_messages
        WHERE account_id = ? AND list_cached = 1
    '''
    params: List[Any] = [account_id]
    if folders:
        placeholders = ', '.join('?' for _ in folders)
        sql += f' AND folder IN ({placeholders})'
        params.extend(folders)
    sql += ' ORDER BY received_at_sort DESC, id DESC'
    database = get_db()
    rows = [dict(row) for row in database.execute(sql, params).fetchall()]

    chosen: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for row in rows:
        identity = retained_mail_identity_key(
            row.get('internet_message_id'),
            row.get('subject'),
            row.get('sender_email'),
            row.get('sender'),
            row.get('received_at_sort'),
            row.get('folder'),
            row.get('provider_message_id'),
            row.get('id_mode'),
        )
        previous = chosen.get(identity)
        if previous is None:
            chosen[identity] = row
            order.append(identity)
            continue
        if _retained_export_row_rank(row) > _retained_export_row_rank(previous):
            chosen[identity] = row

    refs = []
    for identity in order:
        row = chosen[identity]
        label = export_folder_label(str(row.get('folder') or ''))
        refs.append({
            'target': {'label': label, 'folder': row.get('folder') or ''},
            'id': str(row.get('provider_message_id') or ''),
            'subject': str(row.get('subject') or ''),
            'from': str(row.get('sender') or ''),
            'date': str(row.get('received_at') or ''),
            'raw': build_retained_export_eml(row),
        })
    if on_status:
        on_status(f'已找到 {len(refs)} 封', total=len(refs), folder='')
    if not refs:
        return {
            'success': False,
            'cancelled': False,
            'error': '本地还没有这个邮箱的已保留邮件',
            'refs': [],
            'folder_errors': [],
            'truncated': False,
        }
    return {
        'success': True,
        'cancelled': False,
        'error': '',
        'refs': refs,
        'folder_errors': [],
        'truncated': False,
    }


def _run_mailbox_export_job(job_id: str, ctx: MailboxExportContext) -> None:
    with _mailbox_export_lock:
        job = mailbox_export_jobs.get(job_id)
        if not job:
            return
        email_addr = str(job.get('email') or '')
        scope = str(job.get('scope') or 'mailbox')
        folder = str(job.get('folder') or '')
        folder_id = str(job.get('folder_id') or '')
        mailbox = str(job.get('mailbox') or '')
        job['status'] = 'running'
        job['message'] = '正在读取本地邮件'

    if mailbox_export_cancel_requested(job_id):
        _finish_mailbox_export_job(job_id, ctx, status='cancelled', message='导出已取消', error='')
        return

    account = resolve_account_for_email_api(email_addr)
    if not account:
        _finish_mailbox_export_job(job_id, ctx, status='failed', error='账号不存在', message='导出失败')
        return

    def on_status(message: str, total: Optional[int] = None, folder: str = '') -> None:
        fields: Dict[str, Any] = {'message': message, 'current_folder': folder, 'status': 'running'}
        if total is not None:
            fields['total'] = total
        update_mailbox_export_job(job_id, **fields)

    collected = load_local_mailbox_export_refs(
        account,
        scope,
        folder,
        folder_id,
        mailbox,
        lambda: mailbox_export_cancel_requested(job_id),
        on_status,
    )
    if collected.get('cancelled') or mailbox_export_cancel_requested(job_id):
        _finish_mailbox_export_job(job_id, ctx, status='cancelled', message='导出已取消', error='')
        return
    if not collected.get('success'):
        _finish_mailbox_export_job(
            job_id,
            ctx,
            status='failed',
            error=mailbox_export_error_text(collected.get('error'), '没有可导出的邮件'),
            message='导出失败',
            total=0,
        )
        return

    refs = collected.get('refs') or []
    directory = mailbox_export_directory()
    file_descriptor, file_path = tempfile_mkstemp(directory, job_id)
    os.close(file_descriptor)
    update_mailbox_export_job(
        job_id,
        file_path=file_path,
        total=len(refs),
        completed=0,
        message=f'共 {len(refs)} 封，开始写入压缩包',
        truncated=bool(collected.get('truncated')),
    )

    def on_progress(completed: int, total: int, folder_label: str) -> None:
        update_mailbox_export_job(
            job_id,
            completed=completed,
            total=total,
            current_folder=folder_label,
            message=f'正在导出 {completed}/{total}',
            status='running',
        )

    try:
        written = build_mailbox_export_zip(
            file_path,
            account.get('email') or email_addr,
            refs,
            lambda ref: {'success': True, 'raw': ref.get('raw') or b''},
            lambda: mailbox_export_cancel_requested(job_id),
            on_progress,
            truncated=False,
            folder_errors=[],
            max_messages=len(refs),
            scope=scope,
            note=LOCAL_EXPORT_NOTE,
            source='local',
        )
    except Exception:
        _finish_mailbox_export_job(job_id, ctx, status='failed', error='写入压缩包失败', message='导出失败')
        raise

    if written.get('cancelled') or mailbox_export_cancel_requested(job_id):
        _finish_mailbox_export_job(job_id, ctx, status='cancelled', message='导出已取消', error='')
        return
    if not written.get('success'):
        _finish_mailbox_export_job(
            job_id,
            ctx,
            status='failed',
            error=mailbox_export_error_text(written.get('error'), '没有成功导出的邮件'),
            message='导出失败',
            failed_count=int(written.get('failed_count') or 0),
            completed=int(written.get('exported_count') or 0),
        )
        return

    failed_count = int(written.get('failed_count') or 0)
    exported_count = int(written.get('exported_count') or 0)
    message = f'已导出 {exported_count} 封本地邮件'
    if failed_count:
        message += f'，{failed_count} 封失败'
    _finish_mailbox_export_job(
        job_id,
        ctx,
        status='succeeded',
        message=message,
        error='',
        completed=exported_count,
        failed_count=failed_count,
        truncated=False,
        total=len(refs),
        filename=build_export_download_name(account.get('email') or email_addr),
        file_path=file_path,
    )


def tempfile_mkstemp(directory: str, job_id: str):
    import tempfile
    safe_id = re.sub(r'[^A-Za-z0-9_-]+', '', job_id)[:40] or 'export'
    return tempfile.mkstemp(prefix=f'{safe_id}-', suffix='.zip', dir=directory)


def start_mailbox_export_thread(job_id: str) -> None:
    thread = threading.Thread(
        target=run_mailbox_export_job,
        args=(job_id,),
        name=f'mailbox-export-{job_id[:8]}',
        daemon=True,
    )
    thread.start()


def start_mailbox_export_job(account: Dict[str, Any], scope: str, folder: str = '',
                             folder_id: str = '', mailbox: str = '') -> Dict[str, Any]:
    cleanup_mailbox_export_jobs()
    email_addr = str(account.get('email') or '').strip()
    with _mailbox_export_lock:
        running = [
            job for job in mailbox_export_jobs.values()
            if job.get('status') in {'pending', 'running'}
        ]
        same = [
            job for job in running
            if str(job.get('email') or '').strip().lower() == email_addr.lower()
        ]
        if same:
            return {
                'success': False,
                'error': '这个邮箱正在导出，请等待当前任务结束',
                'job_id': same[0].get('job_id') or '',
                'status_code': 409,
            }
        if len(running) >= MAILBOX_EXPORT_MAX_CONCURRENT:
            return {
                'success': False,
                'error': '已有导出任务在进行，请稍后再试',
                'status_code': 409,
            }
        job_id = secrets.token_urlsafe(24)
        mailbox_export_jobs[job_id] = {
            'job_id': job_id,
            'status': 'pending',
            'email': email_addr,
            'scope': scope,
            'folder': folder,
            'folder_id': folder_id,
            'mailbox': mailbox,
            'total': 0,
            'completed': 0,
            'failed_count': 0,
            'current_folder': '',
            'message': '等待开始',
            'error': '',
            'truncated': False,
            'max_messages': MAILBOX_EXPORT_MAX_MESSAGES,
            'filename': build_export_download_name(email_addr),
            'file_path': '',
            'cancel_requested': False,
            'created_at': time_module_now(),
        }
    start_mailbox_export_thread(job_id)
    with _mailbox_export_lock:
        job = mailbox_export_jobs.get(job_id) or {}
        payload = public_mailbox_export_job(job)
    payload['success'] = True
    return payload


def request_mailbox_export_cancel(job_id: str) -> Optional[Dict[str, Any]]:
    with _mailbox_export_lock:
        job = mailbox_export_jobs.get(job_id)
        if not job:
            return None
        if job.get('status') in {'pending', 'running'}:
            job['cancel_requested'] = True
            job['message'] = '正在取消...'
        return public_mailbox_export_job(job)


def _normalize_export_job_id(job_id: str) -> str:
    value = str(job_id or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', value):
        return ''
    return value


def _read_export_request_body() -> Dict[str, Any]:
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return {}
    return data


@app.route('/api/emails/<email_addr>/export', methods=['POST'])
@login_required
def api_start_mailbox_export(email_addr):
    """为单个邮箱创建全部邮件导出任务，完成后下载 ZIP。"""
    account = resolve_account_for_email_api(email_addr)
    if not account:
        return jsonify({'success': False, 'error': '账号不存在'}), 404

    data = _read_export_request_body()
    scope = str(data.get('scope') or 'mailbox').strip().lower()
    if scope not in {'mailbox', 'folder'}:
        return jsonify({'success': False, 'error': 'scope 只能是 mailbox 或 folder'}), 400
    folder = str(data.get('folder') or '').strip()
    folder_id = str(data.get('folder_id') or '').strip()
    mailbox = str(data.get('mailbox') or '').strip()
    if len(folder_id) > 1024 or len(mailbox) > 1024 or len(folder) > 1024:
        return jsonify({'success': False, 'error': '文件夹参数过长'}), 400
    if folder_id and mailbox:
        return jsonify({'success': False, 'error': 'folder_id 与 mailbox 不能同时使用'}), 400

    result = start_mailbox_export_job(account, scope, folder, folder_id, mailbox)
    status_code = int(result.pop('status_code', 200) or 200)
    return jsonify(result), status_code


@app.route('/api/emails/export-jobs/<job_id>', methods=['GET'])
@login_required
def api_get_mailbox_export_job(job_id):
    normalized = _normalize_export_job_id(job_id)
    if not normalized:
        return jsonify({'success': False, 'error': '导出任务不存在'}), 404
    with _mailbox_export_lock:
        job = mailbox_export_jobs.get(normalized)
        payload = public_mailbox_export_job(job) if job else None
    if not payload:
        return jsonify({'success': False, 'error': '导出任务不存在'}), 404
    payload['success'] = True
    response = jsonify(payload)
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/api/emails/export-jobs/<job_id>/cancel', methods=['POST'])
@login_required
def api_cancel_mailbox_export_job(job_id):
    normalized = _normalize_export_job_id(job_id)
    if not normalized:
        return jsonify({'success': False, 'error': '导出任务不存在'}), 404
    payload = request_mailbox_export_cancel(normalized)
    if not payload:
        return jsonify({'success': False, 'error': '导出任务不存在'}), 404
    payload['success'] = True
    return jsonify(payload)


@app.route('/api/emails/export-jobs/<job_id>/download', methods=['GET'])
@login_required
def api_download_mailbox_export_job(job_id):
    normalized = _normalize_export_job_id(job_id)
    if not normalized:
        return jsonify({'success': False, 'error': '导出任务不存在'}), 404
    with _mailbox_export_lock:
        job = mailbox_export_jobs.get(normalized)
        if not job:
            return jsonify({'success': False, 'error': '导出任务不存在'}), 404
        if job.get('status') != 'succeeded' or not job.get('file_path'):
            return jsonify({'success': False, 'error': '导出尚未完成'}), 409
        file_path = str(job.get('file_path') or '')
        filename = str(job.get('filename') or 'mailbox-emails.zip')
    if not os.path.isfile(file_path):
        return jsonify({'success': False, 'error': '导出文件已失效，请重新导出'}), 404
    response = send_file(
        file_path,
        mimetype='application/zip',
        as_attachment=True,
        download_name=filename,
        max_age=0,
        conditional=False,
    )
    response.headers['Cache-Control'] = 'no-store'
    return response
