from __future__ import annotations

import mimetypes
from email.utils import formataddr, parseaddr
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from web_outlook_app import *  # noqa: F403


EMAIL_ADDRESS_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _parse_json_list(value: Any) -> List[str]:
    if value is None or value == '':
        return []
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith('['):
            try:
                parsed = json.loads(text)
                items = parsed if isinstance(parsed, list) else [text]
            except Exception:
                items = re.split(r'[,;\n]+', text)
        else:
            items = re.split(r'[,;\n]+', text)
    else:
        items = [value]
    result = []
    for item in items:
        address = extract_email_address(item)
        if address:
            result.append(address)
    return result


def extract_email_address(value: Any) -> str:
    if isinstance(value, dict):
        nested = value.get('emailAddress') if isinstance(value.get('emailAddress'), dict) else value
        candidate = nested.get('address') or nested.get('email') or ''
        return str(candidate or '').strip().lower()
    text = str(value or '').strip()
    if not text:
        return ''
    _name, addr = parseaddr(text)
    candidate = (addr or text).strip().lower()
    if EMAIL_ADDRESS_RE.match(candidate):
        return candidate
    return ''


def normalize_recipient_list(values: Any, *, required: bool = False, field_name: str = 'to') -> Tuple[List[str], Optional[str]]:
    recipients = []
    seen = set()
    for address in _parse_json_list(values):
        if address in seen:
            continue
        if not EMAIL_ADDRESS_RE.match(address):
            return [], f'{field_name} 含有无效邮箱地址: {address}'
        seen.add(address)
        recipients.append(address)
    if required and not recipients:
        return [], f'{field_name} 不能为空'
    return recipients, None


def html_to_plain_text(body_html: str) -> str:
    text = re.sub(r'(?is)<(script|style).*?>.*?</\1>', '', body_html or '')
    text = re.sub(r'(?i)<br\s*/?>', '\n', text)
    text = re.sub(r'(?i)</p\s*>', '\n', text)
    text = re.sub(r'(?is)<[^>]+>', '', text)
    text = html.unescape(text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def ensure_subject_prefix(subject: str, prefix: str) -> str:
    normalized = str(subject or '').strip() or '(无主题)'
    if re.match(rf'(?i)^{re.escape(prefix)}\s*', normalized):
        return normalized
    return f'{prefix} {normalized}'


def validate_compose_attachments(files: List[Any]) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    attachments = []
    total_size = 0
    for uploaded in files or []:
        if uploaded is None:
            continue
        filename = PurePosixPath(str(getattr(uploaded, 'filename', '') or '')).name.strip()
        if not filename:
            continue
        ext = Path(filename).suffix.lower()
        if ext in EMAIL_ATTACHMENT_BLOCKED_EXTENSIONS:
            return [], build_error_payload(
                'EMAIL_ATTACHMENT_BLOCKED',
                f'不允许上传该类型附件: {filename}',
                'ValidationError',
                400,
            )
        data = uploaded.read()
        size = len(data or b'')
        if size <= 0:
            return [], build_error_payload(
                'EMAIL_ATTACHMENT_EMPTY',
                f'附件为空: {filename}',
                'ValidationError',
                400,
            )
        if size > EMAIL_ATTACHMENT_MAX_BYTES:
            return [], build_error_payload(
                'EMAIL_ATTACHMENT_TOO_LARGE',
                f'单个附件不能超过 {EMAIL_ATTACHMENT_MAX_BYTES // (1024 * 1024)}MB: {filename}',
                'ValidationError',
                400,
            )
        total_size += size
        if total_size > EMAIL_ATTACHMENT_TOTAL_MAX_BYTES:
            return [], build_error_payload(
                'EMAIL_ATTACHMENT_TOTAL_TOO_LARGE',
                f'附件总大小不能超过 {EMAIL_ATTACHMENT_TOTAL_MAX_BYTES // (1024 * 1024)}MB',
                'ValidationError',
                400,
            )
        content_type = (
            getattr(uploaded, 'mimetype', None)
            or mimetypes.guess_type(filename)[0]
            or 'application/octet-stream'
        )
        attachments.append({
            'filename': filename,
            'content_type': content_type,
            'content_bytes': data,
            'size': size,
        })
    return attachments, None


def build_graph_recipients(addresses: List[str]) -> List[Dict[str, Any]]:
    return [{'emailAddress': {'address': address}} for address in addresses]


def build_graph_file_attachments(attachments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    for item in attachments:
        result.append({
            '@odata.type': '#microsoft.graph.fileAttachment',
            'name': item['filename'],
            'contentType': item.get('content_type') or 'application/octet-stream',
            'contentBytes': base64.b64encode(item['content_bytes']).decode('ascii'),
        })
    return result


def resolve_account_smtp_config(account: Dict[str, Any]) -> Dict[str, Any]:
    provider = account.get('provider') or 'custom'
    email_addr = account.get('email') or ''
    provider_meta = get_provider_meta(provider, email_addr)
    host = str(account.get('smtp_host') or '').strip()
    try:
        port = int(account.get('smtp_port') or 0)
    except (TypeError, ValueError):
        port = 0
    use_tls = bool(account.get('smtp_use_tls'))
    use_ssl = bool(account.get('smtp_use_ssl'))
    if not host:
        host = str(provider_meta.get('smtp_host') or '').strip()
        port = int(provider_meta.get('smtp_port') or 0)
        use_tls = bool(provider_meta.get('smtp_use_tls', False))
        use_ssl = bool(provider_meta.get('smtp_use_ssl', False))
    if not port:
        port = 465 if use_ssl else 587
    return {
        'host': host,
        'port': port,
        'use_tls': use_tls,
        'use_ssl': use_ssl,
        'username': email_addr,
        'password': account.get('imap_password') or '',
        'from_email': email_addr,
    }


def build_smtp_email_message(
    *,
    from_email: str,
    to_list: List[str],
    cc_list: List[str],
    bcc_list: List[str],
    subject: str,
    body_html: str,
    body_text: str,
    attachments: List[Dict[str, Any]],
    in_reply_to: str = '',
    references: str = '',
) -> EmailMessage:
    message = EmailMessage()
    message['From'] = from_email
    message['To'] = ', '.join(to_list)
    if cc_list:
        message['Cc'] = ', '.join(cc_list)
    if bcc_list:
        message['Bcc'] = ', '.join(bcc_list)
    message['Subject'] = subject
    if in_reply_to:
        message['In-Reply-To'] = in_reply_to
    if references:
        message['References'] = references

    plain = body_text or html_to_plain_text(body_html) or ''
    message.set_content(plain or '')
    if body_html:
        message.add_alternative(body_html, subtype='html')

    for item in attachments:
        maintype, _, subtype = (item.get('content_type') or 'application/octet-stream').partition('/')
        if not subtype:
            maintype, subtype = 'application', 'octet-stream'
        message.add_attachment(
            item['content_bytes'],
            maintype=maintype,
            subtype=subtype,
            filename=item['filename'],
        )
    return message


def send_smtp_message(config: Dict[str, Any], message: EmailMessage, proxy_url: str = '') -> Dict[str, Any]:
    host = str(config.get('host') or '').strip()
    if not host:
        return {
            'success': False,
            'error': build_error_payload(
                'SMTP_CONFIG_REQUIRED',
                '当前账号未配置 SMTP，无法发信',
                'ValidationError',
                400,
            ),
        }
    password = str(config.get('password') or '')
    username = str(config.get('username') or '')
    if not password:
        return {
            'success': False,
            'error': build_error_payload(
                'SMTP_AUTH_REQUIRED',
                '缺少 IMAP/SMTP 密码，无法发信',
                'ValidationError',
                400,
            ),
        }

    port = int(config.get('port') or 465)
    use_ssl = bool(config.get('use_ssl'))
    use_tls = bool(config.get('use_tls'))
    try:
        with proxy_socket_context(proxy_url):
            smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
            with smtp_cls(host, port, timeout=30) as client:
                if not use_ssl:
                    client.ehlo()
                    if use_tls:
                        client.starttls()
                        client.ehlo()
                client.login(username, password)
                client.send_message(message)
        return {'success': True}
    except smtplib.SMTPAuthenticationError as exc:
        return {
            'success': False,
            'error': build_error_payload(
                'SMTP_AUTH_FAILED',
                'SMTP 认证失败，请检查应用专用密码/授权码是否支持 SMTP',
                'SMTPError',
                401,
                str(exc),
            ),
        }
    except Exception as exc:
        return {
            'success': False,
            'error': build_error_payload(
                'SMTP_SEND_FAILED',
                'SMTP 发信失败',
                'SMTPError',
                502,
                str(exc),
            ),
        }


def is_graph_mail_send_forbidden(details: Any) -> bool:
    text = ''
    if isinstance(details, dict):
        try:
            text = json.dumps(details, ensure_ascii=True).lower()
        except Exception:
            text = str(details).lower()
        error = details.get('error') if isinstance(details.get('error'), dict) else {}
        code = str(error.get('code') or details.get('code') or '').lower()
        if code in {'accessdenied', 'forbidden', 'authorization_requestdenied', 'invalidauthenticationtoken'}:
            if 'mail.send' in text or 'insufficient privileges' in text or 'access is denied' in text:
                return True
    else:
        text = str(details or '').lower()
    return any(marker in text for marker in (
        'mail.send',
        'insufficient privileges',
        'accessdenied',
        'not granted consent',
        'authorization_requestdenied',
    ))


def graph_send_json(access_token: str, url: str, payload: Optional[Dict[str, Any]],
                    proxy_url: str = None, fallback_proxy_urls: Optional[List[str]] = None,
                    method: str = 'post') -> Any:
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
    }
    return request_with_proxy_failover(
        method,
        url,
        headers=headers,
        json=payload,
        timeout=max(HTTP_REQUEST_TIMEOUT, 60),
        proxy_url=proxy_url,
        fallback_proxy_urls=fallback_proxy_urls,
    )


def send_email_via_graph(
    account: Dict[str, Any],
    *,
    to_list: List[str],
    cc_list: List[str],
    bcc_list: List[str],
    subject: str,
    body_html: str,
    body_text: str,
    attachments: List[Dict[str, Any]],
    proxy_url: str = None,
    fallback_proxy_urls: Optional[List[str]] = None,
) -> Dict[str, Any]:
    token_result = get_access_token_graph_result(
        account.get('client_id', ''),
        account.get('refresh_token', ''),
        proxy_url,
        fallback_proxy_urls,
    )
    if not token_result.get('success'):
        return {'success': False, 'error': token_result.get('error')}

    access_token = token_result['access_token']
    content = body_html if body_html else (body_text or '')
    content_type = 'HTML' if body_html else 'Text'
    message = {
        'subject': subject,
        'body': {'contentType': content_type, 'content': content},
        'toRecipients': build_graph_recipients(to_list),
    }
    if cc_list:
        message['ccRecipients'] = build_graph_recipients(cc_list)
    if bcc_list:
        message['bccRecipients'] = build_graph_recipients(bcc_list)
    if attachments:
        message['attachments'] = build_graph_file_attachments(attachments)

    response = graph_send_json(
        access_token,
        'https://graph.microsoft.com/v1.0/me/sendMail',
        {'message': message, 'saveToSentItems': True},
        proxy_url,
        fallback_proxy_urls,
    )
    if response.status_code in {200, 202}:
        return {'success': True}
    details = get_response_details(response)
    if response.status_code in {401, 403} and is_graph_mail_send_forbidden(details):
        return {
            'success': False,
            'error': build_error_payload(
                'GRAPH_MAIL_SEND_SCOPE_REQUIRED',
                '当前账号缺少 Mail.Send 权限，请重新授权后再发信',
                'GraphAPIError',
                response.status_code,
                details,
            ),
        }
    return {
        'success': False,
        'error': build_error_payload(
            'GRAPH_SEND_FAILED',
            'Graph 发信失败',
            'GraphAPIError',
            response.status_code,
            details,
        ),
    }


def _graph_create_and_send(
    access_token: str,
    *,
    message_id: str,
    action: str,
    body_html: str,
    body_text: str,
    to_list: Optional[List[str]],
    cc_list: Optional[List[str]],
    bcc_list: Optional[List[str]],
    subject: Optional[str],
    attachments: List[Dict[str, Any]],
    proxy_url: str = None,
    fallback_proxy_urls: Optional[List[str]] = None,
) -> Dict[str, Any]:
    create_url = f'https://graph.microsoft.com/v1.0/me/messages/{message_id}/{action}'
    create_response = graph_send_json(
        access_token, create_url, {}, proxy_url, fallback_proxy_urls
    )
    if create_response.status_code not in {200, 201}:
        details = get_response_details(create_response)
        if create_response.status_code in {401, 403} and is_graph_mail_send_forbidden(details):
            return {
                'success': False,
                'error': build_error_payload(
                    'GRAPH_MAIL_SEND_SCOPE_REQUIRED',
                    '当前账号缺少 Mail.Send 权限，请重新授权后再发信',
                    'GraphAPIError',
                    create_response.status_code,
                    details,
                ),
            }
        return {
            'success': False,
            'error': build_error_payload(
                'GRAPH_CREATE_DRAFT_FAILED',
                f'创建{action} 草稿失败',
                'GraphAPIError',
                create_response.status_code,
                details,
            ),
        }

    draft = create_response.json() if create_response.content else {}
    draft_id = draft.get('id')
    if not draft_id:
        return {
            'success': False,
            'error': build_error_payload(
                'GRAPH_CREATE_DRAFT_FAILED',
                '创建草稿成功但未返回草稿 ID',
                'GraphAPIError',
                502,
            ),
        }

    patch_payload: Dict[str, Any] = {}
    content = body_html if body_html else (body_text or '')
    if content:
        patch_payload['body'] = {
            'contentType': 'HTML' if body_html else 'Text',
            'content': content,
        }
    if subject is not None:
        patch_payload['subject'] = subject
    if to_list is not None:
        patch_payload['toRecipients'] = build_graph_recipients(to_list)
    if cc_list is not None:
        patch_payload['ccRecipients'] = build_graph_recipients(cc_list)
    if bcc_list is not None:
        patch_payload['bccRecipients'] = build_graph_recipients(bcc_list)

    if patch_payload:
        patch_response = graph_send_json(
            access_token,
            f'https://graph.microsoft.com/v1.0/me/messages/{draft_id}',
            patch_payload,
            proxy_url,
            fallback_proxy_urls,
            method='patch',
        )
        if patch_response.status_code not in {200, 202}:
            return {
                'success': False,
                'error': build_error_payload(
                    'GRAPH_UPDATE_DRAFT_FAILED',
                    '更新草稿失败',
                    'GraphAPIError',
                    patch_response.status_code,
                    get_response_details(patch_response),
                ),
            }

    for item in attachments:
        attach_payload = {
            '@odata.type': '#microsoft.graph.fileAttachment',
            'name': item['filename'],
            'contentType': item.get('content_type') or 'application/octet-stream',
            'contentBytes': base64.b64encode(item['content_bytes']).decode('ascii'),
        }
        attach_response = graph_send_json(
            access_token,
            f'https://graph.microsoft.com/v1.0/me/messages/{draft_id}/attachments',
            attach_payload,
            proxy_url,
            fallback_proxy_urls,
        )
        if attach_response.status_code not in {200, 201}:
            return {
                'success': False,
                'error': build_error_payload(
                    'GRAPH_ATTACH_FAILED',
                    f"添加附件失败: {item['filename']}",
                    'GraphAPIError',
                    attach_response.status_code,
                    get_response_details(attach_response),
                ),
            }

    send_response = graph_send_json(
        access_token,
        f'https://graph.microsoft.com/v1.0/me/messages/{draft_id}/send',
        None,
        proxy_url,
        fallback_proxy_urls,
    )
    # send endpoint expects empty body; requests with json=None may still send null
    if send_response.status_code == 400:
        send_response = request_with_proxy_failover(
            'post',
            f'https://graph.microsoft.com/v1.0/me/messages/{draft_id}/send',
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Length': '0',
            },
            data=b'',
            timeout=max(HTTP_REQUEST_TIMEOUT, 60),
            proxy_url=proxy_url,
            fallback_proxy_urls=fallback_proxy_urls,
        )

    if send_response.status_code in {200, 202}:
        return {'success': True, 'message_id': draft_id}
    details = get_response_details(send_response)
    if send_response.status_code in {401, 403} and is_graph_mail_send_forbidden(details):
        return {
            'success': False,
            'error': build_error_payload(
                'GRAPH_MAIL_SEND_SCOPE_REQUIRED',
                '当前账号缺少 Mail.Send 权限，请重新授权后再发信',
                'GraphAPIError',
                send_response.status_code,
                details,
            ),
        }
    return {
        'success': False,
        'error': build_error_payload(
            'GRAPH_SEND_FAILED',
            '发送草稿失败',
            'GraphAPIError',
            send_response.status_code,
            details,
        ),
    }


def reply_email_via_graph(
    account: Dict[str, Any],
    *,
    message_id: str,
    reply_all: bool,
    body_html: str,
    body_text: str,
    to_list: Optional[List[str]],
    cc_list: Optional[List[str]],
    bcc_list: Optional[List[str]],
    attachments: List[Dict[str, Any]],
    proxy_url: str = None,
    fallback_proxy_urls: Optional[List[str]] = None,
) -> Dict[str, Any]:
    token_result = get_access_token_graph_result(
        account.get('client_id', ''),
        account.get('refresh_token', ''),
        proxy_url,
        fallback_proxy_urls,
    )
    if not token_result.get('success'):
        return {'success': False, 'error': token_result.get('error')}

    action = 'createReplyAll' if reply_all else 'createReply'
    result = _graph_create_and_send(
        token_result['access_token'],
        message_id=message_id,
        action=action,
        body_html=body_html,
        body_text=body_text,
        to_list=to_list,
        cc_list=cc_list,
        bcc_list=bcc_list,
        subject=None,
        attachments=attachments,
        proxy_url=proxy_url,
        fallback_proxy_urls=fallback_proxy_urls,
    )
    if result.get('success'):
        return result

    # Fallback: compose a new mail with Re: subject
    detail_result = get_email_detail_graph_result(
        account.get('client_id', ''),
        account.get('refresh_token', ''),
        message_id,
        proxy_url,
        fallback_proxy_urls,
    )
    if not detail_result.get('success'):
        return result
    detail = detail_result.get('detail') or {}
    subject = ensure_subject_prefix(detail.get('subject') or '', 'Re:')
    fallback_to = to_list
    if not fallback_to:
        sender = extract_email_address(detail.get('from'))
        fallback_to = [sender] if sender else []
    if not fallback_to:
        return result
    quoted = detail.get('body', {}).get('content') if isinstance(detail.get('body'), dict) else ''
    combined_html = body_html or ''
    if quoted:
        combined_html = f"{combined_html}<br><hr><blockquote>{quoted}</blockquote>"
    return send_email_via_graph(
        account,
        to_list=fallback_to,
        cc_list=cc_list or [],
        bcc_list=bcc_list or [],
        subject=subject,
        body_html=combined_html,
        body_text=body_text,
        attachments=attachments,
        proxy_url=proxy_url,
        fallback_proxy_urls=fallback_proxy_urls,
    )


def forward_email_via_graph(
    account: Dict[str, Any],
    *,
    message_id: str,
    to_list: List[str],
    cc_list: List[str],
    bcc_list: List[str],
    body_html: str,
    body_text: str,
    subject: str,
    attachments: List[Dict[str, Any]],
    proxy_url: str = None,
    fallback_proxy_urls: Optional[List[str]] = None,
) -> Dict[str, Any]:
    token_result = get_access_token_graph_result(
        account.get('client_id', ''),
        account.get('refresh_token', ''),
        proxy_url,
        fallback_proxy_urls,
    )
    if not token_result.get('success'):
        return {'success': False, 'error': token_result.get('error')}

    result = _graph_create_and_send(
        token_result['access_token'],
        message_id=message_id,
        action='createForward',
        body_html=body_html,
        body_text=body_text,
        to_list=to_list,
        cc_list=cc_list,
        bcc_list=bcc_list,
        subject=subject or None,
        attachments=attachments,
        proxy_url=proxy_url,
        fallback_proxy_urls=fallback_proxy_urls,
    )
    if result.get('success'):
        return result

    detail_result = get_email_detail_graph_result(
        account.get('client_id', ''),
        account.get('refresh_token', ''),
        message_id,
        proxy_url,
        fallback_proxy_urls,
    )
    if not detail_result.get('success'):
        return result
    detail = detail_result.get('detail') or {}
    forward_subject = subject or ensure_subject_prefix(detail.get('subject') or '', 'Fw:')
    quoted = detail.get('body', {}).get('content') if isinstance(detail.get('body'), dict) else ''
    combined_html = body_html or ''
    if quoted:
        combined_html = f"{combined_html}<br><hr><blockquote>{quoted}</blockquote>"
    return send_email_via_graph(
        account,
        to_list=to_list,
        cc_list=cc_list,
        bcc_list=bcc_list,
        subject=forward_subject,
        body_html=combined_html,
        body_text=body_text,
        attachments=attachments,
        proxy_url=proxy_url,
        fallback_proxy_urls=fallback_proxy_urls,
    )


def send_email_via_account_smtp(
    account: Dict[str, Any],
    *,
    to_list: List[str],
    cc_list: List[str],
    bcc_list: List[str],
    subject: str,
    body_html: str,
    body_text: str,
    attachments: List[Dict[str, Any]],
    proxy_url: str = '',
    in_reply_to: str = '',
    references: str = '',
) -> Dict[str, Any]:
    config = resolve_account_smtp_config(account)
    message = build_smtp_email_message(
        from_email=config['from_email'],
        to_list=to_list,
        cc_list=cc_list,
        bcc_list=bcc_list,
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        attachments=attachments,
        in_reply_to=in_reply_to,
        references=references,
    )
    return send_smtp_message(config, message, proxy_url=proxy_url or '')


def parse_compose_request(require_message_id: bool = False) -> Tuple[Optional[Dict[str, Any]], Optional[Any]]:
    if request.content_type and 'multipart/form-data' in request.content_type:
        form = request.form
        data = {
            'email': form.get('email', ''),
            'to': form.get('to', '[]'),
            'cc': form.get('cc', '[]'),
            'bcc': form.get('bcc', '[]'),
            'subject': form.get('subject', ''),
            'body_html': form.get('body_html', ''),
            'body_text': form.get('body_text', ''),
            'message_id': form.get('message_id', ''),
            'folder': form.get('folder', 'inbox'),
            'method': form.get('method', ''),
            'reply_all': form.get('reply_all', 'false'),
        }
        files = request.files.getlist('attachments')
    else:
        data = request.get_json(silent=True) or {}
        files = []

    email_addr = str(data.get('email') or '').strip()
    if not email_addr:
        return None, (jsonify({'success': False, 'error': 'email 不能为空'}), 400)

    account = get_account_by_email(email_addr)
    if not account:
        return None, (jsonify({'success': False, 'error': '账号不存在'}), 404)
    if int(account.get('group_id') or 0) == TEMP_EMAIL_GROUP_ID:
        return None, _error_response(build_error_payload(
            'TEMP_EMAIL_SEND_UNSUPPORTED',
            '临时邮箱不支持发信',
            'ValidationError',
            400,
        ))

    to_list, to_error = normalize_recipient_list(data.get('to'), required=False, field_name='to')
    if to_error:
        return None, (jsonify({'success': False, 'error': to_error}), 400)
    cc_list, cc_error = normalize_recipient_list(data.get('cc'), required=False, field_name='cc')
    if cc_error:
        return None, (jsonify({'success': False, 'error': cc_error}), 400)
    bcc_list, bcc_error = normalize_recipient_list(data.get('bcc'), required=False, field_name='bcc')
    if bcc_error:
        return None, (jsonify({'success': False, 'error': bcc_error}), 400)

    attachments, attach_error = validate_compose_attachments(files)
    if attach_error:
        return None, _error_response(attach_error)

    message_id = str(data.get('message_id') or '').strip()
    if require_message_id and not message_id:
        return None, (jsonify({'success': False, 'error': 'message_id 不能为空'}), 400)

    body_html = str(data.get('body_html') or '')
    body_text = str(data.get('body_text') or '')
    if not body_html and not body_text and not attachments and not require_message_id:
        # allow empty body for reply/forward with quoted original; for new mail require content or attachment
        pass

    return {
        'account': account,
        'email': email_addr,
        'to': to_list,
        'cc': cc_list,
        'bcc': bcc_list,
        'subject': str(data.get('subject') or '').strip(),
        'body_html': body_html,
        'body_text': body_text,
        'message_id': message_id,
        'folder': str(data.get('folder') or 'inbox').strip() or 'inbox',
        'method': str(data.get('method') or '').strip().lower(),
        'reply_all': _coerce_bool(data.get('reply_all'), False),
        'attachments': attachments,
    }, None


def _error_response(error_payload: Any, default_status: int = 400):
    if isinstance(error_payload, dict) and error_payload.get('code'):
        status = int(error_payload.get('status') or default_status)
        return jsonify({
            'success': False,
            'error': error_payload.get('message') or '发信失败',
            'code': error_payload.get('code'),
            'details': error_payload,
        }), status
    return jsonify({'success': False, 'error': error_payload or '发信失败'}), default_status


@app.route('/api/emails/send', methods=['POST'])
@login_required
def api_send_email():
    parsed, error_response = parse_compose_request(require_message_id=False)
    if error_response:
        return error_response

    to_list = parsed['to']
    if not to_list:
        return jsonify({'success': False, 'error': 'to 不能为空'}), 400
    if not parsed['subject'] and not parsed['body_html'] and not parsed['body_text'] and not parsed['attachments']:
        return jsonify({'success': False, 'error': '主题、正文或附件至少填写一项'}), 400

    account = parsed['account']
    proxy_url = get_account_proxy_url(account)
    fallback_proxy_urls = get_account_proxy_failover_urls(account)
    subject = parsed['subject'] or '(无主题)'

    if account.get('account_type') == 'outlook' or account.get('provider') == 'outlook':
        result = send_email_via_graph(
            account,
            to_list=to_list,
            cc_list=parsed['cc'],
            bcc_list=parsed['bcc'],
            subject=subject,
            body_html=parsed['body_html'],
            body_text=parsed['body_text'],
            attachments=parsed['attachments'],
            proxy_url=proxy_url,
            fallback_proxy_urls=fallback_proxy_urls,
        )
    else:
        result = send_email_via_account_smtp(
            account,
            to_list=to_list,
            cc_list=parsed['cc'],
            bcc_list=parsed['bcc'],
            subject=subject,
            body_html=parsed['body_html'],
            body_text=parsed['body_text'],
            attachments=parsed['attachments'],
            proxy_url=proxy_url,
        )

    if not result.get('success'):
        return _error_response(result.get('error'))
    return jsonify({'success': True, 'message': '邮件已发送', 'message_id': result.get('message_id')})


@app.route('/api/emails/reply', methods=['POST'])
@login_required
def api_reply_email():
    parsed, error_response = parse_compose_request(require_message_id=True)
    if error_response:
        return error_response

    account = parsed['account']
    proxy_url = get_account_proxy_url(account)
    fallback_proxy_urls = get_account_proxy_failover_urls(account)
    reply_all = parsed['reply_all']

    if account.get('account_type') == 'outlook' or account.get('provider') == 'outlook':
        result = reply_email_via_graph(
            account,
            message_id=parsed['message_id'],
            reply_all=reply_all,
            body_html=parsed['body_html'],
            body_text=parsed['body_text'],
            to_list=parsed['to'] or None,
            cc_list=parsed['cc'] or None,
            bcc_list=parsed['bcc'] or None,
            attachments=parsed['attachments'],
            proxy_url=proxy_url,
            fallback_proxy_urls=fallback_proxy_urls,
        )
    else:
        to_list = parsed['to']
        if not to_list:
            return jsonify({'success': False, 'error': '回复时 to 不能为空'}), 400
        subject = parsed['subject'] or 'Re:'
        if not re.match(r'(?i)^re:', subject):
            subject = ensure_subject_prefix(subject, 'Re:')
        result = send_email_via_account_smtp(
            account,
            to_list=to_list,
            cc_list=parsed['cc'],
            bcc_list=parsed['bcc'],
            subject=subject,
            body_html=parsed['body_html'],
            body_text=parsed['body_text'],
            attachments=parsed['attachments'],
            proxy_url=proxy_url,
            in_reply_to=parsed['message_id'],
            references=parsed['message_id'],
        )

    if not result.get('success'):
        return _error_response(result.get('error'))
    return jsonify({'success': True, 'message': '回复已发送', 'message_id': result.get('message_id')})


@app.route('/api/emails/forward', methods=['POST'])
@login_required
def api_forward_email():
    parsed, error_response = parse_compose_request(require_message_id=True)
    if error_response:
        return error_response

    to_list = parsed['to']
    if not to_list:
        return jsonify({'success': False, 'error': '转发时 to 不能为空'}), 400

    account = parsed['account']
    proxy_url = get_account_proxy_url(account)
    fallback_proxy_urls = get_account_proxy_failover_urls(account)
    subject = parsed['subject']
    if subject and not re.match(r'(?i)^(fw|fwd):', subject):
        subject = ensure_subject_prefix(subject, 'Fw:')

    if account.get('account_type') == 'outlook' or account.get('provider') == 'outlook':
        result = forward_email_via_graph(
            account,
            message_id=parsed['message_id'],
            to_list=to_list,
            cc_list=parsed['cc'],
            bcc_list=parsed['bcc'],
            body_html=parsed['body_html'],
            body_text=parsed['body_text'],
            subject=subject,
            attachments=parsed['attachments'],
            proxy_url=proxy_url,
            fallback_proxy_urls=fallback_proxy_urls,
        )
    else:
        if not subject:
            subject = 'Fw:'
        result = send_email_via_account_smtp(
            account,
            to_list=to_list,
            cc_list=parsed['cc'],
            bcc_list=parsed['bcc'],
            subject=subject,
            body_html=parsed['body_html'],
            body_text=parsed['body_text'],
            attachments=parsed['attachments'],
            proxy_url=proxy_url,
        )

    if not result.get('success'):
        return _error_response(result.get('error'))
    return jsonify({'success': True, 'message': '转发已发送', 'message_id': result.get('message_id')})
