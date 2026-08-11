"""单账号邮箱目录树：Graph / IMAP 枚举 + 短缓存 + folders API。"""

from __future__ import annotations

import imaplib
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

from flask import jsonify, request


MAIL_FOLDER_CACHE_TTL_SECONDS = 600
MAIL_FOLDER_CACHE_MAX_ENTRIES = 200

_mail_folder_cache_lock = threading.Lock()
_mail_folder_cache: Dict[int, Dict[str, Any]] = {}


def clear_mail_folder_cache(account_id: Optional[int] = None) -> None:
    with _mail_folder_cache_lock:
        if account_id is None:
            _mail_folder_cache.clear()
            return
        _mail_folder_cache.pop(int(account_id), None)


def _store_mail_folder_cache(account_id: int, payload: Dict[str, Any]) -> None:
    with _mail_folder_cache_lock:
        if len(_mail_folder_cache) >= MAIL_FOLDER_CACHE_MAX_ENTRIES and account_id not in _mail_folder_cache:
            oldest_key = min(
                _mail_folder_cache.items(),
                key=lambda item: float(item[1].get('cached_at') or 0),
            )[0]
            _mail_folder_cache.pop(oldest_key, None)
        _mail_folder_cache[int(account_id)] = {
            'cached_at': time.time(),
            'payload': payload,
        }


def _get_mail_folder_cache(account_id: int) -> Optional[Dict[str, Any]]:
    with _mail_folder_cache_lock:
        entry = _mail_folder_cache.get(int(account_id))
        if not entry:
            return None
        age = time.time() - float(entry.get('cached_at') or 0)
        if age > MAIL_FOLDER_CACHE_TTL_SECONDS:
            _mail_folder_cache.pop(int(account_id), None)
            return None
        payload = entry.get('payload')
        return dict(payload) if isinstance(payload, dict) else None


def build_imap_folder_tree_nodes(entries: List[Dict[str, Any]], provider: str = 'imap') -> List[Dict[str, Any]]:
    """将扁平 IMAP LIST 结果组装为带 parent_id 的节点列表。"""
    nodes_by_id: Dict[str, Dict[str, Any]] = {}
    well_known_by_name = {
        'inbox': 'inbox',
        'junk': 'junkemail',
        'junkemail': 'junkemail',
        'spam': 'junkemail',
        'deleted': 'deleteditems',
        'deleted items': 'deleteditems',
        'trash': 'deleteditems',
        'sent': 'sentitems',
        'sent items': 'sentitems',
        'drafts': 'drafts',
    }

    for entry in entries or []:
        mailbox = str(entry.get('name') or '').strip()
        if not mailbox:
            continue
        delimiter = str(entry.get('delimiter') or '/')
        attrs = [str(item).lower().lstrip('\\') for item in (entry.get('attributes') or [])]
        selectable = 'noselect' not in attrs
        display_name = decode_imap_utf7(mailbox)
        if delimiter:
            parts = [part for part in mailbox.split(delimiter) if part]
        else:
            parts = [mailbox]
        if not parts:
            continue

        parent_id = None
        path_parts = []
        for index, part in enumerate(parts):
            path_parts.append(part)
            node_id = delimiter.join(path_parts) if delimiter else part
            is_leaf = index == len(parts) - 1
            if node_id not in nodes_by_id:
                terminal_display = decode_imap_utf7(part)
                nodes_by_id[node_id] = {
                    'id': node_id,
                    'name': part,
                    'display_name': terminal_display if not is_leaf or len(parts) > 1 else display_name,
                    'parent_id': parent_id,
                    'has_children': False,
                    'selectable': False,
                    'well_known': None,
                    'provider': provider,
                    'mailbox': node_id,
                }
            node = nodes_by_id[node_id]
            if parent_id and parent_id in nodes_by_id:
                nodes_by_id[parent_id]['has_children'] = True
            if is_leaf:
                node['selectable'] = selectable
                node['mailbox'] = mailbox
                node['display_name'] = decode_imap_utf7(mailbox.split(delimiter)[-1] if delimiter else mailbox)
                normalized_terminal = normalize_imap_mailbox_name(node['display_name'])
                node['well_known'] = well_known_by_name.get(normalized_terminal)
            parent_id = node_id

    return sorted(
        nodes_by_id.values(),
        key=lambda item: (
            0 if item.get('parent_id') in (None, '') else 1,
            str(item.get('display_name') or '').lower(),
            str(item.get('id') or ''),
        ),
    )


def _graph_folder_page(access_token: str, url: str, proxy_url: str = '',
                       fallback_proxy_urls: Optional[List[str]] = None) -> Dict[str, Any]:
    headers = {
        'Authorization': f'Bearer {access_token}',
    }
    res = get_with_proxy_fallback(
        url,
        headers=headers,
        timeout=HTTP_REQUEST_TIMEOUT,
        proxy_url=proxy_url,
        fallback_proxy_urls=fallback_proxy_urls,
    )
    if res.status_code != 200:
        details = get_response_details(res)
        return {
            'success': False,
            'error': build_error_payload(
                'FOLDER_LIST_FAILED',
                '获取文件夹列表失败',
                'GraphAPIError',
                res.status_code,
                details,
            ),
        }
    payload = res.json() if res.content else {}
    return {
        'success': True,
        'value': payload.get('value') or [],
        'next': payload.get('@odata.nextLink') or '',
    }


def list_graph_mail_folder_nodes(account: Dict[str, Any]) -> Dict[str, Any]:
    proxy_url = get_account_proxy_url(account)
    fallback_proxy_urls = get_account_proxy_failover_urls(account)
    token_result = get_access_token_graph_result(
        account.get('client_id'),
        account.get('refresh_token'),
        proxy_url,
        fallback_proxy_urls,
    )
    if not token_result.get('success'):
        return {'success': False, 'error': token_result.get('error') or '获取 Graph token 失败'}

    access_token = token_result.get('access_token')
    select = 'id,displayName,parentFolderId,childFolderCount'
    root_url = (
        'https://graph.microsoft.com/v1.0/me/mailFolders'
        f'?$top=100&$select={select}'
    )

    folders: List[Dict[str, Any]] = []
    next_url = root_url
    seen_urls = set()
    while next_url and next_url not in seen_urls:
        seen_urls.add(next_url)
        page = _graph_folder_page(access_token, next_url, proxy_url, fallback_proxy_urls)
        if not page.get('success'):
            return page
        folders.extend(page.get('value') or [])
        next_url = page.get('next') or ''

    # 递归拉取子文件夹（按 childFolderCount）
    queue_ids = [
        str(item.get('id') or '').strip()
        for item in folders
        if int(item.get('childFolderCount') or 0) > 0 and str(item.get('id') or '').strip()
    ]
    visited_children: Set[str] = set()
    while queue_ids:
        folder_id = queue_ids.pop(0)
        if not folder_id or folder_id in visited_children:
            continue
        visited_children.add(folder_id)
        child_url = (
            f'https://graph.microsoft.com/v1.0/me/mailFolders/{quote(folder_id, safe="")}/childFolders'
            f'?$top=100&$select={select}'
        )
        child_next = child_url
        child_seen = set()
        while child_next and child_next not in child_seen:
            child_seen.add(child_next)
            page = _graph_folder_page(access_token, child_next, proxy_url, fallback_proxy_urls)
            if not page.get('success'):
                return page
            for child in page.get('value') or []:
                folders.append(child)
                child_id = str(child.get('id') or '').strip()
                if child_id and int(child.get('childFolderCount') or 0) > 0:
                    queue_ids.append(child_id)
            child_next = page.get('next') or ''

    id_set = {
        str(item.get('id') or '').strip()
        for item in folders
        if str(item.get('id') or '').strip()
    }
    well_known_names = {
        'inbox': 'inbox',
        'junkemail': 'junkemail',
        'deleteditems': 'deleteditems',
        'sentitems': 'sentitems',
        'drafts': 'drafts',
        'archive': 'archive',
    }
    nodes = []
    for item in folders:
        folder_id = str(item.get('id') or '').strip()
        if not folder_id:
            continue
        parent_id = str(item.get('parentFolderId') or '').strip() or None
        if parent_id and parent_id not in id_set:
            parent_id = None
        display_name = str(item.get('displayName') or '').strip() or folder_id
        normalized_name = normalize_imap_mailbox_name(display_name)
        nodes.append({
            'id': folder_id,
            'name': display_name,
            'display_name': display_name,
            'parent_id': parent_id,
            'has_children': int(item.get('childFolderCount') or 0) > 0,
            'selectable': True,
            'well_known': well_known_names.get(normalized_name),
            'provider': 'graph',
            'folder_id': folder_id,
        })

    return {
        'success': True,
        'folders': nodes,
        'provider': 'graph',
    }


def list_imap_mail_folder_nodes_for_generic(account: Dict[str, Any]) -> Dict[str, Any]:
    proxy_url = get_account_proxy_url(account) or ''
    mail = None
    try:
        mail = create_imap_connection(
            account.get('imap_host', ''),
            account.get('imap_port', 993),
            proxy_url,
        )
        mail.login(account.get('email', ''), account.get('imap_password', ''))
        provider = account.get('provider', 'custom')
        send_imap_id(mail, provider, account.get('imap_host', ''))
        entries = list_imap_mailbox_entries(mail)
        return {
            'success': True,
            'folders': build_imap_folder_tree_nodes(entries, provider='imap'),
            'provider': 'imap',
        }
    except Exception as exc:
        return {
            'success': False,
            'error': f'IMAP 文件夹列表失败: {sanitize_error_details(str(exc))[:200]}',
        }
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass


def list_imap_mail_folder_nodes_for_outlook(account: Dict[str, Any]) -> Dict[str, Any]:
    proxy_url = get_account_proxy_url(account)
    fallback_proxy_urls = get_account_proxy_failover_urls(account)
    token_result = get_access_token_imap_result(
        account.get('client_id'),
        account.get('refresh_token'),
        proxy_url,
        fallback_proxy_urls,
    )
    if not token_result.get('success'):
        return {'success': False, 'error': token_result.get('error') or '获取 IMAP token 失败'}

    access_token = token_result.get('access_token')
    errors = []
    for server in (IMAP_SERVER_NEW, IMAP_SERVER_OLD):
        connection = None
        try:
            with proxy_socket_context(proxy_url):
                connection = imaplib.IMAP4_SSL(server, IMAP_PORT, timeout=IMAP_TIMEOUT)
            auth_string = f"user={account.get('email')}\1auth=Bearer {access_token}\1\1".encode('utf-8')
            connection.authenticate('XOAUTH2', lambda x: auth_string)
            entries = list_imap_mailbox_entries(connection)
            return {
                'success': True,
                'folders': build_imap_folder_tree_nodes(entries, provider='imap'),
                'provider': 'imap',
                'imap_server': server,
            }
        except Exception as exc:
            errors.append(f'{server}: {sanitize_error_details(str(exc))[:120]}')
        finally:
            if connection is not None:
                try:
                    connection.logout()
                except Exception:
                    pass
    return {
        'success': False,
        'error': 'IMAP 文件夹列表失败',
        'details': errors,
    }


def list_account_mail_folders(account: Dict[str, Any], force_refresh: bool = False) -> Dict[str, Any]:
    account_id = int(account.get('id') or 0)
    if not force_refresh and account_id:
        cached = _get_mail_folder_cache(account_id)
        if cached is not None:
            cached = dict(cached)
            cached['cached'] = True
            return cached

    if account.get('account_type') == 'imap':
        result = list_imap_mail_folder_nodes_for_generic(account)
    else:
        result = list_graph_mail_folder_nodes(account)
        if not result.get('success'):
            graph_error = result.get('error')
            fallback = list_imap_mail_folder_nodes_for_outlook(account)
            if fallback.get('success'):
                result = fallback
                result['graph_error'] = graph_error

    if result.get('success'):
        payload = {
            'success': True,
            'folders': result.get('folders') or [],
            'provider': result.get('provider'),
            'cached': False,
        }
        if account_id:
            _store_mail_folder_cache(account_id, payload)
        return payload
    return {
        'success': False,
        'error': result.get('error') or '获取文件夹失败',
        'details': result.get('details'),
    }


def collect_allowed_mail_folder_refs(account: Dict[str, Any],
                                     force_refresh: bool = False) -> Tuple[Set[str], Set[str]]:
    result = list_account_mail_folders(account, force_refresh=force_refresh)
    folder_ids: Set[str] = set()
    mailboxes: Set[str] = set()
    if not result.get('success'):
        return folder_ids, mailboxes
    for node in result.get('folders') or []:
        folder_id = str(node.get('folder_id') or '').strip()
        mailbox = str(node.get('mailbox') or '').strip()
        node_id = str(node.get('id') or '').strip()
        if folder_id:
            folder_ids.add(folder_id)
        if mailbox:
            mailboxes.add(mailbox)
        if node.get('provider') == 'imap' and node_id:
            mailboxes.add(node_id)
        if node.get('provider') == 'graph' and node_id:
            folder_ids.add(node_id)
    return folder_ids, mailboxes


def validate_mail_folder_ref(account: Dict[str, Any], folder_id: str = '',
                             mailbox: str = '') -> Optional[str]:
    explicit_folder_id = str(folder_id or '').strip()
    explicit_mailbox = str(mailbox or '').strip()
    if not explicit_folder_id and not explicit_mailbox:
        return None
    if explicit_folder_id and explicit_mailbox:
        return 'folder_id 与 mailbox 不能同时使用'
    folder_ids, mailboxes = collect_allowed_mail_folder_refs(account, force_refresh=False)
    if not folder_ids and not mailboxes:
        # 缓存/列表失败时再强制刷新一次
        folder_ids, mailboxes = collect_allowed_mail_folder_refs(account, force_refresh=True)
    if explicit_folder_id and explicit_folder_id not in folder_ids:
        return 'folder_id 无效或不属于当前账号'
    if explicit_mailbox and explicit_mailbox not in mailboxes:
        return 'mailbox 无效或不属于当前账号'
    return None


def parse_mail_folder_request_args(args=None) -> Dict[str, Any]:
    source = args if args is not None else request.args
    folder_id = str(source.get('folder_id') or '').strip()
    mailbox = str(source.get('mailbox') or '').strip()
    folder = normalize_folder_name(source.get('folder', 'inbox'))

    if folder.startswith('graph:') and not folder_id:
        folder_id = folder[6:]
        folder = 'inbox'
    elif folder.startswith('imap:') and not mailbox:
        mailbox = folder[5:]
        folder = 'inbox'

    if folder_id and mailbox:
        return {
            'ok': False,
            'error': 'folder_id 与 mailbox 不能同时使用',
            'status': 400,
        }

    if folder_id:
        return {
            'ok': True,
            'kind': 'graph',
            'folder': build_graph_folder_storage_key(folder_id),
            'folder_id': folder_id,
            'mailbox': '',
            'storage_folder': build_graph_folder_storage_key(folder_id),
        }
    if mailbox:
        return {
            'ok': True,
            'kind': 'imap',
            'folder': build_imap_folder_storage_key(mailbox),
            'folder_id': '',
            'mailbox': mailbox,
            'storage_folder': build_imap_folder_storage_key(mailbox),
        }
    if folder not in VALID_MAIL_FOLDERS:
        return {
            'ok': False,
            'error': f'folder 参数无效，支持: {", ".join(sorted(VALID_MAIL_FOLDERS))}',
            'status': 400,
        }
    return {
        'ok': True,
        'kind': 'well_known',
        'folder': folder,
        'folder_id': '',
        'mailbox': '',
        'storage_folder': folder,
    }


@app.route('/api/emails/<email_addr>/folders', methods=['GET'])
@login_required
def api_get_email_folders(email_addr):
    requested_email = str(email_addr or '').strip()
    account = resolve_account_for_email_api(requested_email)
    if not account:
        return jsonify({'success': False, 'error': '账号不存在'}), 404

    force_refresh = str(request.args.get('refresh') or '').strip().lower() in {
        '1', 'true', 'yes', 'on'
    }
    result = list_account_mail_folders(account, force_refresh=force_refresh)
    if not result.get('success'):
        return jsonify({
            'success': False,
            'error': result.get('error') or '获取文件夹失败',
            'details': result.get('details'),
        }), 502

    return jsonify({
        'success': True,
        'email': account.get('email'),
        'requested_email': requested_email,
        'provider': result.get('provider'),
        'cached': bool(result.get('cached')),
        'folders': result.get('folders') or [],
    })
