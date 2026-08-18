from __future__ import annotations

import json
import os
import queue
import re
import threading
import urllib.parse
import uuid
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from flask import stream_with_context

if TYPE_CHECKING:
    from web_outlook_app import *  # noqa: F403


# ==================== Graph OAuth 自动提取 ====================

# client_id 和 redirect_uri 复用 01_bootstrap.py 中的 OAUTH_CLIENT_ID / OAUTH_REDIRECT_URI，
# 不再单独定义 GRAPH_EXTRACT_CLIENT_ID / GRAPH_EXTRACT_REDIRECT_URI 环境变量。
# scope 和 authority 为 Graph 自动提取专用，值与常规 OAuth 不同，保持独立。
GRAPH_EXTRACT_SCOPE = os.getenv(
    "GRAPH_EXTRACT_SCOPE",
    "offline_access https://outlook.office.com/IMAP.AccessAsUser.All",
)
# GraphAPI：与 OAUTH_GRAPH_SCOPES 对齐，含读信 / 标已读写权限 / 发信 / User.Read
GRAPH_EXTRACT_GRAPH_SCOPE = os.getenv(
    "GRAPH_EXTRACT_GRAPH_SCOPE",
    " ".join(["offline_access", *OAUTH_GRAPH_SCOPES]),
)
GRAPH_EXTRACT_AUTHORITY = os.getenv("GRAPH_EXTRACT_AUTHORITY", "consumers")
GRAPH_EXTRACT_SCOPE_BY_MODE = {
    "imap": GRAPH_EXTRACT_SCOPE,
    "graph": GRAPH_EXTRACT_GRAPH_SCOPE,
}

# Backward-compatible aliases for existing docs/tests.
GRAPH_CLIENT_ID = OAUTH_CLIENT_ID
GRAPH_REDIRECT_URI = OAUTH_REDIRECT_URI
GRAPH_SCOPE = GRAPH_EXTRACT_SCOPE

GRAPH_OAUTH_TASKS: Dict[str, Dict[str, Any]] = {}
GRAPH_OAUTH_DONE = object()


def normalize_graph_oauth_mode(mode: Any) -> str:
    normalized = str(mode or "graph").strip().lower()
    return normalized if normalized in GRAPH_EXTRACT_SCOPE_BY_MODE else "graph"


def graph_oauth_mode_label(mode: str) -> str:
    return "GraphAPI" if mode == "graph" else "IMAP授权"


def graph_oauth_sse(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def graph_oauth_safe_details(details: Any) -> str:
    return sanitize_error_details(str(details or ""))[:500]


def graph_oauth_log(log: Optional[Callable[[str], None]], message: str) -> None:
    if log:
        log(graph_oauth_safe_details(message))


def make_graph_oauth_response(success: bool, error: str = "", details: str = "",
                              **extra: Any) -> Dict[str, Any]:
    payload = {"success": bool(success)}
    if error:
        payload["error"] = graph_oauth_safe_details(error)
    if details:
        payload["details"] = graph_oauth_safe_details(details)
    payload.update(extra)
    return payload


def build_graph_authorize_url(client_id: str, redirect_uri: str, scope: str,
                              authority: str) -> str:
    return (
        f"https://login.microsoftonline.com/{authority}/oauth2/v2.0/authorize"
        f"?client_id={urllib.parse.quote(client_id, safe='')}"
        f"&response_type=code"
        f"&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
        f"&scope={urllib.parse.quote(scope)}"
        f"&response_mode=query"
    )


def make_light_response(url: str, text: str = "", status_code: int = 200):
    return type("GraphOauthResponse", (), {
        "url": url,
        "text": text,
        "status_code": status_code,
        "headers": {},
    })()


def extract_hidden_inputs(html: str) -> Dict[str, str]:
    return {
        name: value
        for name, value in re.findall(
            r'<input[^>]*name="([^"]*)"[^>]*value="([^"]*)"',
            html or "",
            re.IGNORECASE,
        )
    }


def absolute_form_action(action: str, current_url: str) -> str:
    action = (action or "").replace("&amp;", "&")
    if action.startswith("http"):
        return action
    base = urllib.parse.urlparse(current_url)
    if action.startswith("/"):
        return f"{base.scheme}://{base.netloc}{action}"
    path = urllib.parse.urljoin(f"{base.scheme}://{base.netloc}{base.path}", action)
    return path


def extract_named_inputs(html: str) -> Dict[str, Dict[str, str]]:
    """提取带 name 的 input，返回 name -> {type, value, id}。"""
    inputs: Dict[str, Dict[str, str]] = {}
    for match in re.finditer(r'<input\b([^>]*)>', html or '', re.IGNORECASE):
        attrs = match.group(1) or ''
        name_match = re.search(r'\bname=["\']([^"\']+)["\']', attrs, re.IGNORECASE)
        if not name_match:
            continue
        name = name_match.group(1)
        type_match = re.search(r'\btype=["\']([^"\']+)["\']', attrs, re.IGNORECASE)
        value_match = re.search(r'\bvalue=["\']([^"\']*)["\']', attrs, re.IGNORECASE)
        id_match = re.search(r'\bid=["\']([^"\']+)["\']', attrs, re.IGNORECASE)
        inputs[name] = {
            'type': (type_match.group(1) if type_match else 'text').lower(),
            'value': value_match.group(1) if value_match else '',
            'id': id_match.group(1) if id_match else '',
        }
    return inputs


def pick_proof_email_field(inputs: Dict[str, Dict[str, str]]) -> Optional[str]:
    for name, meta in inputs.items():
        lowered = name.lower()
        input_type = meta.get('type') or ''
        if input_type in {'hidden', 'submit', 'button', 'checkbox', 'radio', 'password'}:
            continue
        if input_type == 'email':
            return name
        if any(token in lowered for token in ('email', 'proof', 'altemail', 'recovery')):
            return name
    return None


def pick_proof_code_field(inputs: Dict[str, Dict[str, str]]) -> Optional[str]:
    for name, meta in inputs.items():
        lowered = name.lower()
        input_type = meta.get('type') or ''
        if input_type in {'hidden', 'submit', 'button', 'checkbox', 'radio', 'password', 'email'}:
            continue
        if any(token in lowered for token in ('otc', 'ott', 'code', 'verify', 'verification')):
            return name
    # 常见备用：单文本框页面
    text_fields = [
        name for name, meta in inputs.items()
        if (meta.get('type') or 'text') in {'text', 'tel', 'number'}
        and name.lower() not in {'action', 'canary', 'hpgrequestid'}
    ]
    if len(text_fields) == 1:
        return text_fields[0]
    return None


def html_looks_like_proof_code_page(url: str, html: str) -> bool:
    text = html or ''
    url_l = (url or '').lower()
    if any(token in url_l for token in ('proofs/verify', 'proofs/confirm', 'interrupt/proofs')):
        return True
    if re.search(r'name=["\'][^"\']*(otc|ott|VerificationCode|security.?code)[^"\']*["\']', text, re.I):
        return True
    if re.search(r'(enter|输入).{0,20}(code|验证码)', text, re.I):
        return True
    return False


def build_proof_skip_form_data(form_html: str) -> Dict[str, str]:
    form_data = extract_hidden_inputs(form_html)
    form_data['action'] = 'Skip'
    return form_data


def follow_oauth_redirects(session: Any, resp2: Any, *, max_hops: int = 8) -> Any:
    hops = 0
    while getattr(resp2, 'status_code', None) in (301, 302, 303, 307) and hops < max_hops:
        loc = (getattr(resp2, 'headers', None) or {}).get('Location', '')
        if not loc:
            break
        if 'localhost' in loc:
            return make_light_response(loc)
        resp2 = session.get(loc, timeout=30, allow_redirects=False)
        hops += 1
    return resp2


def try_bind_microsoft_proof_email(
    session: Any,
    *,
    current_url: str,
    form_action: str,
    form_html: str,
    account_email: str,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[Any]:
    """尝试用 HFMail 代填并代绑辅助邮箱。成功返回下一跳响应，失败返回 None（调用方应 Skip）。"""
    try:
        with app.app_context():
            if not is_hfmail_configured():
                return None

            recovery_email, create_error = hfmail_create_mailbox(account_email)
            if not recovery_email:
                graph_oauth_log(log, f"HFMail 创建辅助邮箱失败，回退 Skip: {create_error}")
                return None

            inputs = extract_named_inputs(form_html)
            email_field = pick_proof_email_field(inputs)
            if not email_field:
                graph_oauth_log(log, "安全信息页未找到邮箱输入框，回退 Skip")
                return None

            since = hfmail_utc_now_iso()
            form_data = extract_hidden_inputs(form_html)
            form_data[email_field] = recovery_email
            # 常见提交动作；若页面已有 action 且不是 Skip，保留
            existing_action = str(form_data.get('action') or '').strip()
            if not existing_action or existing_action.lower() == 'skip':
                form_data['action'] = 'AddProof'
            graph_oauth_log(log, f"提交 Microsoft 辅助邮箱: {recovery_email}")

            resp2 = session.post(
                form_action,
                data=form_data,
                timeout=30,
                allow_redirects=False,
            )
            resp2 = follow_oauth_redirects(session, resp2)

            # 最多再处理两轮验证码/确认页
            for _ in range(2):
                next_url = getattr(resp2, 'url', '') or ''
                next_html = getattr(resp2, 'text', '') or ''
                if 'localhost' in next_url and 'code=' in next_url:
                    return resp2
                if not html_looks_like_proof_code_page(next_url, next_html):
                    # 已离开 proofs 验证页，视为绑定流程走通（或被重定向到下一 OAuth 步）
                    if 'proofs/add' not in next_url.lower():
                        return resp2
                    # 仍停在 Add 页且没有验证码框，视为失败
                    return None

                code_form_match = re.search(
                    r'<form[^>]*action="([^"]+)"[^>]*>(.*?)</form>',
                    next_html,
                    re.DOTALL | re.IGNORECASE,
                )
                if not code_form_match:
                    graph_oauth_log(log, "验证码页面缺少表单，回退 Skip")
                    return None

                code_inputs = extract_named_inputs(code_form_match.group(2))
                code_field = pick_proof_code_field(code_inputs)
                if not code_field:
                    graph_oauth_log(log, "验证码页面未找到验证码输入框，回退 Skip")
                    return None

                graph_oauth_log(log, f"等待 HFMail 验证码: {recovery_email}")
                code, code_error, _payload = hfmail_wait_verification_code(
                    recovery_email,
                    since=since,
                    sender_contains='microsoft',
                    total_timeout_seconds=90,
                    wait_seconds_per_call=25,
                )
                if not code:
                    graph_oauth_log(log, f"获取辅助邮箱验证码失败，回退 Skip: {code_error}")
                    return None

                code_form_data = extract_hidden_inputs(code_form_match.group(2))
                code_form_data[code_field] = code
                if not str(code_form_data.get('action') or '').strip():
                    code_form_data['action'] = 'VerifyProof'
                graph_oauth_log(log, "提交 Microsoft 辅助邮箱验证码")
                resp2 = session.post(
                    absolute_form_action(code_form_match.group(1), next_url or current_url),
                    data=code_form_data,
                    timeout=30,
                    allow_redirects=False,
                )
                resp2 = follow_oauth_redirects(session, resp2)

            return resp2
    except Exception as exc:
        graph_oauth_log(log, f"辅助邮箱代绑异常，回退 Skip: {type(exc).__name__}: {exc}")
        return None


def extract_graph_refresh_token(
    email: str,
    password: str,
    *,
    client_id: str = OAUTH_CLIENT_ID,
    redirect_uri: str = OAUTH_REDIRECT_URI,
    scope: str = GRAPH_EXTRACT_SCOPE,
    authority: str = GRAPH_EXTRACT_AUTHORITY,
    log: Optional[Callable[[str], None]] = None,
    session_factory: Optional[Callable[[], Any]] = None,
    proxy_url: str = None,
) -> Dict[str, Any]:
    """使用纯 HTTP OAuth2 授权码流程提取 Outlook refresh_token。"""
    try:
        session = session_factory() if session_factory else requests.Session()
        resolved_proxy = str(proxy_url or '').strip()
        if resolved_proxy:
            proxies = build_proxies(resolved_proxy)
            if proxies:
                session.proxies.update(proxies)
            # 已配置应用代理时避免与环境代理叠加
            session.trust_env = False
        else:
            session.trust_env = True
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            )
        })

        graph_oauth_log(log, f"获取 Microsoft 授权页面: {email}")
        log_outbound_proxy_usage(f'Outlook自动授权 {email}', resolved_proxy or '')
        if resolved_proxy:
            graph_oauth_log(log, f"OAuth 全程固定代理: {format_proxy_for_log(resolved_proxy)}")
        resp = session.get(
            build_graph_authorize_url(client_id, redirect_uri, scope, authority),
            timeout=30,
            allow_redirects=True,
        )
        text = resp.text or ""

        flow_token = ""
        sft_tag = re.search(r'sFTTag.*?value=\\?"([^"\\]+)', text, re.DOTALL)
        if sft_tag:
            flow_token = sft_tag.group(1)
        if not flow_token:
            ppft = re.search(r'name="PPFT"[^>]*value="([^"]+)"', text, re.IGNORECASE)
            if ppft:
                flow_token = ppft.group(1)
        if not flow_token:
            return make_graph_oauth_response(False, "无法提取 Flow Token", "未在授权页面找到 PPFT 字段")

        post_url = ""
        urlpost_match = re.search(r'"urlPost"\s*:\s*"([^"]+)"', text)
        if urlpost_match:
            post_url = urlpost_match.group(1).replace("\\u0026", "&")
        if not post_url:
            post_url = "https://login.live.com/ppsecure/post.srf"

        ctx = ""
        sctx_match = re.search(r'"sCtx"\s*:\s*"([^"]+)"', text)
        if sctx_match:
            ctx = sctx_match.group(1)

        graph_oauth_log(log, "提交 Microsoft 登录凭据")
        resp2 = session.post(
            post_url,
            data={
                "login": email,
                "loginfmt": email,
                "passwd": password,
                "PPFT": flow_token,
                "ctx": ctx,
                "type": "11",
                "LoginOptions": "3",
                "i13": "0",
                "CookieDisclosure": "0",
                "IsFidoSupported": "0",
                "isSignupPost": "0",
                "i19": "16393",
            },
            timeout=30,
            allow_redirects=False,
        )

        # 检测登录失败的情况
        post_html = resp2.text or ""
        post_url_check = getattr(resp2, "url", "") or post_url

        if resp2.status_code == 200 and "ppsecure/post.srf" in post_url_check:
            # 情况1：检查JavaScript错误变量和HTML错误元素
            error_markers = [
                (r'sErrTxt["\s:=]+["\']([^"\']+)', "JavaScript错误信息"),
                (r'<div[^>]*id=["\']error["\'][^>]*>([^<]+)', "错误提示框"),
                (r'data-bind=["\']text:\s*unsafe_(\w+)["\']', "验证失败"),
                (r'<div[^>]*class=["\'][^"\']*error[^"\']*["\'][^>]*>([^<]+)', "错误样式"),
            ]

            for pattern, error_type in error_markers:
                match = re.search(pattern, post_html, re.IGNORECASE | re.DOTALL)
                if match:
                    error_detail = match.group(1).strip() if match.lastindex and len(match.groups()) > 0 else error_type
                    # 清理HTML标签
                    error_detail = re.sub(r'<[^>]+>', '', error_detail).strip()
                    return make_graph_oauth_response(
                        False,
                        "Microsoft 登录失败",
                        f"{error_type}: {graph_oauth_safe_details(error_detail)}"
                    )

            # 情况2：没有重定向且停留在post.srf，检查是否返回了登录表单
            if not resp2.headers.get("Location"):
                # 如果页面包含密码输入框，说明登录失败返回了登录页面
                if re.search(r'name=["\']passwd["\']', post_html, re.IGNORECASE):
                    # 尝试提取更具体的错误信息
                    specific_errors = [
                        (r'incorrect|invalid|wrong', "密码不正确或账号不存在"),
                        (r'verify|verification|confirm', "需要额外验证"),
                        (r'suspicious|unusual', "检测到异常活动"),
                        (r'disabled|locked|blocked', "账号被锁定或禁用"),
                    ]

                    error_hint = "密码不正确、账号不存在或需要额外验证"
                    for pattern, hint in specific_errors:
                        if re.search(pattern, post_html, re.IGNORECASE):
                            error_hint = hint
                            break

                    return make_graph_oauth_response(
                        False,
                        "登录凭据验证失败",
                        f"提交凭据后返回了登录表单，通常表示{error_hint}。请手动登录 https://outlook.live.com 确认账号状态。"
                    )

        for _ in range(5):
            html = resp2.text or ""
            if ("DoSubmit" in html or ("fmHF" in html and "onload" in html)) and "action=" in html:
                form_action_match = re.search(r'action="([^"]+)"', html)
                if form_action_match:
                    form_action = form_action_match.group(1).replace("&amp;", "&")
                    graph_oauth_log(log, "处理 Microsoft 中间自动提交页面")
                    resp2 = session.post(
                        form_action,
                        data=extract_hidden_inputs(html),
                        timeout=30,
                        allow_redirects=False,
                    )
                    continue
            break

        auth_code = None
        for _ in range(15):
            while resp2.status_code in (301, 302, 303, 307):
                loc = resp2.headers.get("Location", "")
                if "localhost" in loc:
                    resp2 = make_light_response(loc)
                    break
                resp2 = session.get(loc, timeout=30, allow_redirects=False)

            current_url = getattr(resp2, "url", "") or ""
            text = resp2.text if getattr(resp2, "text", "") else ""

            if "localhost" in current_url and "code=" in current_url:
                params = urllib.parse.parse_qs(urllib.parse.urlparse(current_url).query)
                auth_code = params.get("code", [None])[0]
                if auth_code:
                    graph_oauth_log(log, "已捕获授权码")
                    break

            if "localhost" in current_url and "error" in current_url:
                params = urllib.parse.parse_qs(urllib.parse.urlparse(current_url).query)
                err = params.get("error_description", params.get("error", ["?"]))[0]
                return make_graph_oauth_response(False, "OAuth 错误", err)

            if "Consent/Update" in current_url or "Consent/update" in current_url:
                server_data = re.search(r'ServerData\s*=\s*(\{.*?\});', text, re.DOTALL)
                if not server_data:
                    return make_graph_oauth_response(False, "同意页面处理失败", "无法解析 ServerData")
                graph_oauth_log(log, "接受 Outlook 授权同意页面")
                sd = json.loads(server_data.group(1))
                resp2 = session.post(
                    current_url,
                    data={
                        "ucaction": "Yes",
                        "client_id": sd.get("sClientId", ""),
                        "scope": sd.get("sRawInputScopes", ""),
                        "cscope": sd.get("sRawInputGrantedScopes", ""),
                        "canary": sd.get("sCanary", ""),
                    },
                    timeout=30,
                    allow_redirects=False,
                )
                continue

            if "proofs/Add" in current_url or "proofs/add" in current_url:
                form_match = re.search(
                    r'<form[^>]*action="([^"]+)"[^>]*>(.*?)</form>',
                    text,
                    re.DOTALL | re.IGNORECASE,
                )
                if not form_match:
                    return make_graph_oauth_response(False, "安全信息页面处理失败", "无法找到表单")
                form_action = absolute_form_action(form_match.group(1), current_url)
                form_html = form_match.group(2)
                bind_resp = try_bind_microsoft_proof_email(
                    session,
                    current_url=current_url,
                    form_action=form_action,
                    form_html=form_html,
                    account_email=email,
                    log=log,
                )
                if bind_resp is not None:
                    resp2 = bind_resp
                    continue

                graph_oauth_log(log, "跳过 Microsoft 安全信息添加页面")
                resp2 = session.post(
                    form_action,
                    data=build_proof_skip_form_data(form_html),
                    timeout=30,
                    allow_redirects=False,
                )
                continue

            form_match = re.search(
                r'<form[^>]*action="([^"]+)"[^>]*>(.*?)</form>',
                text,
                re.DOTALL | re.IGNORECASE,
            )
            if form_match:
                form_action = absolute_form_action(form_match.group(1), current_url)
                form_data = extract_hidden_inputs(form_match.group(2))
                if "consent" in form_action.lower() or "consent" in current_url.lower():
                    graph_oauth_log(log, "提交通用同意表单")
                    form_data["ucaccept"] = "Yes"

                resp2 = session.post(form_action, data=form_data, timeout=30, allow_redirects=False)
                while resp2.status_code in (301, 302, 303, 307):
                    loc = resp2.headers.get("Location", "")
                    if "localhost" in loc:
                        resp2 = make_light_response(loc)
                        break
                    if not loc:
                        break
                    resp2 = session.get(loc, timeout=30, allow_redirects=False)
                continue

            return make_graph_oauth_response(
                False,
                "授权流程卡住",
                f"在 {current_url[:100]} 无法继续 (status={resp2.status_code})",
            )

        if not auth_code:
            return make_graph_oauth_response(False, "未能获取授权码", "完成所有步骤但未捕获到授权码")

        graph_oauth_log(log, "使用授权码换取 Outlook token")
        token_resp = session.post(
            f"https://login.microsoftonline.com/{authority}/oauth2/v2.0/token",
            data={
                "client_id": client_id,
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": redirect_uri,
                "scope": scope,
            },
            timeout=30,
        )
        token_data = token_resp.json()

        if "access_token" not in token_data:
            err = token_data.get("error_description", token_data.get("error", "?"))
            return make_graph_oauth_response(False, "Token 换取失败", err)

        refresh_token = str(token_data.get("refresh_token") or "").strip()
        if not refresh_token:
            return make_graph_oauth_response(False, "未获取到 refresh_token", "响应中包含 access_token 但没有 refresh_token")

        graph_oauth_log(log, "已获取 Outlook refresh_token")
        return {
            "success": True,
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
    except Exception as exc:
        return make_graph_oauth_response(False, f"异常: {type(exc).__name__}", str(exc))


def get_upload_account_for_graph_auth(account_id: int):
    db = get_db()
    return db.execute(
        '''
        SELECT id, email, password, is_authorized, remark, group_id, proxy_url, tag_ids
        FROM outlook_upload_accounts
        WHERE id = ?
        ''',
        (account_id,),
    ).fetchone()


def upsert_graph_authorized_account(email: str, password: str, client_id: str,
                                    refresh_token: str, *,
                                    group_id: Any = None,
                                    proxy_url: str = '',
                                    tag_ids: Any = None,
                                    remark: str = '',
                                    authorization_type: Optional[str] = None) -> Dict[str, Any]:
    db = get_db()
    existing = db.execute(
        'SELECT id, authorization_type FROM accounts WHERE LOWER(email) = ? LIMIT 1',
        (normalize_email_address(email),),
    ).fetchone()
    encrypted_password = encrypt_data(password) if password else password
    encrypted_refresh_token = encrypt_data(refresh_token) if refresh_token else refresh_token
    if authorization_type is None:
        normalized_authorization_type = normalize_outlook_authorization_type(
            existing['authorization_type'] if existing else ''
        )
    else:
        normalized_authorization_type = normalize_outlook_authorization_type(
            authorization_type,
            strict=True,
        )

    if existing:
        account_id = int(existing['id'])
        # 已有正式账号：仅覆盖授权相关字段，保留分组/标签/代理等业务字段
        db.execute(
            '''
            UPDATE accounts
            SET password = ?,
                client_id = ?,
                refresh_token = ?,
                account_type = 'outlook',
                provider = 'outlook',
                authorization_type = ?,
                refresh_token_updated_at = CURRENT_TIMESTAMP,
                last_refresh_status = 'never',
                last_refresh_error = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            ''',
            (encrypted_password, client_id, encrypted_refresh_token, normalized_authorization_type, account_id),
        )
        return {"account_id": account_id, "created": False}

    resolved_group_id = resolve_upload_group_id(group_id)
    normalized_proxy = str(proxy_url or '').strip()
    cursor = db.execute(ACCOUNT_INSERT_SQL, build_account_insert_values(
        normalize_email_address(email),
        password,
        client_id,
        refresh_token,
        resolved_group_id,
        remark or '',
        'outlook',
        'outlook',
        IMAP_SERVER_NEW,
        IMAP_PORT,
        '',
        False,
        None,
        'active',
        normalized_proxy,
        '',
        '',
    ))
    account_id = int(cursor.lastrowid)
    apply_account_tag_ids(account_id, tag_ids, db)
    db.execute(
        '''
        UPDATE accounts
        SET authorization_type = ?,
            refresh_token_updated_at = CURRENT_TIMESTAMP,
            last_refresh_status = 'never',
            last_refresh_error = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        ''',
        (normalized_authorization_type, account_id),
    )
    return {"account_id": account_id, "created": True}


def mark_upload_account_authorized(account_id: int) -> None:
    get_db().execute(
        '''
        UPDATE outlook_upload_accounts
        SET is_authorized = 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        ''',
        (account_id,),
    )


def save_graph_authorization_result(upload_row: Any, client_id: str,
                                    refresh_token: str,
                                    authorization_type: Optional[str] = None) -> Dict[str, Any]:
    email = str(upload_row['email'] or '').strip()
    password = get_upload_account_plain_password(upload_row)
    row_data = dict(upload_row) if hasattr(upload_row, 'keys') else {}
    save_result = upsert_graph_authorized_account(
        email,
        password,
        client_id,
        refresh_token,
        group_id=row_data.get('group_id'),
        proxy_url=row_data.get('proxy_url') or '',
        tag_ids=decode_upload_tag_ids(row_data.get('tag_ids')),
        remark=str(row_data.get('remark') or ''),
        authorization_type=authorization_type,
    )
    mark_upload_account_authorized(int(upload_row['id']))
    get_db().commit()
    return save_result


def run_graph_oauth_task(account_id: int, output_queue: "queue.Queue[Dict[str, Any] | object]",
                         mode: str = "graph") -> None:
    def emit(payload: Dict[str, Any]) -> None:
        output_queue.put(payload)

    def log(message: str) -> None:
        emit({"type": "log", "message": graph_oauth_safe_details(message)})

    with app.app_context():
        try:
            mode = normalize_graph_oauth_mode(mode)
            upload_row = get_upload_account_for_graph_auth(account_id)
            if not upload_row:
                emit({"type": "error", "success": False, "mode": mode, "message": "上传账号不存在"})
                emit({"type": "complete", "success": False})
                return

            email = str(upload_row['email'] or '').strip()
            password = get_upload_account_plain_password(upload_row)
            if not email or not password:
                emit({"type": "error", "success": False, "mode": mode, "message": "邮箱或密码为空"})
                emit({"type": "complete", "success": False})
                return

            mode_label = graph_oauth_mode_label(mode)
            scope = GRAPH_EXTRACT_SCOPE_BY_MODE[mode]
            proxy_config = get_upload_account_resolved_proxy_config(upload_row)
            # OAuth 多跳必须固定同一主代理；不做中途 failover
            auth_proxy_url = proxy_config.get('proxy_url', '') or ''
            emit({
                "type": "start",
                "email": email,
                "mode": mode,
                "message": f"开始 {mode_label} OAuth 授权",
            })
            log(f"授权模式: {mode_label}")
            log(f"授权 Scope: {scope}")
            if auth_proxy_url:
                log("使用上传账号/分组代理进行自动授权")
            result = extract_graph_refresh_token(
                email,
                password,
                scope=scope,
                log=log,
                proxy_url=auth_proxy_url,
            )
            if not result.get("success"):
                emit({
                    "type": "error",
                    "success": False,
                    "mode": mode,
                    "message": graph_oauth_safe_details(result.get("error") or "授权失败"),
                    "details": graph_oauth_safe_details(result.get("details") or ""),
                })
                emit({"type": "complete", "success": False})
                return

            client_id = str(result.get("client_id") or "").strip()
            refresh_token = str(result.get("refresh_token") or "").strip()
            log(f"验证 {mode_label} refresh_token")
            refresh_result = test_refresh_token(
                client_id,
                refresh_token,
                proxy_url=auth_proxy_url,
                authorization_type=mode,
            )
            try:
                ok, error_msg, rotated_refresh_token, actual_channel = refresh_result
            except (TypeError, ValueError):
                ok, error_msg, rotated_refresh_token = refresh_result
                actual_channel = mode
            actual_channel = normalize_outlook_authorization_type(actual_channel)
            if not ok:
                emit({
                    "type": "error",
                    "success": False,
                    "mode": mode,
                    "message": f"{mode_label} refresh_token 验证失败",
                    "details": graph_oauth_safe_details(error_msg),
                })
                emit({"type": "complete", "success": False})
                return

            token_to_save = rotated_refresh_token or refresh_token
            save_result = save_graph_authorization_result(
                upload_row,
                client_id,
                token_to_save,
                authorization_type=actual_channel or mode,
            )
            emit({
                "type": "success",
                "success": True,
                "mode": mode,
                "authorization_type": actual_channel or mode,
                "email": email,
                "account_id": save_result["account_id"],
                "created": save_result["created"],
                "client_id": client_id,
                "message": "授权成功，已保存到正式账号",
            })
            emit({"type": "complete", "success": True})
        except Exception as exc:
            try:
                get_db().rollback()
            except Exception:
                pass
            emit({
                "type": "error",
                "success": False,
                "mode": normalize_graph_oauth_mode(mode),
                "message": "授权任务异常",
                "details": graph_oauth_safe_details(str(exc)),
            })
            emit({"type": "complete", "success": False})
        finally:
            output_queue.put(GRAPH_OAUTH_DONE)


@app.route('/api/oauth/graph-extract-token', methods=['POST'])
@login_required
def api_graph_extract_token():
    data = request.get_json(silent=True) or {}
    raw_account_id = data.get('account_id')
    try:
        account_id = int(raw_account_id)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'account_id 不能为空'}), 400

    row = get_upload_account_for_graph_auth(account_id)
    if not row:
        return jsonify({'success': False, 'error': '上传账号不存在'}), 404
    if not str(row['email'] or '').strip() or not str(row['password'] or ''):
        return jsonify({'success': False, 'error': '邮箱或密码为空'}), 400

    mode = normalize_graph_oauth_mode(data.get('mode'))
    task_id = uuid.uuid4().hex
    GRAPH_OAUTH_TASKS[task_id] = {'account_id': account_id, 'mode': mode}
    return jsonify({
        'success': True,
        'task_id': task_id,
        'mode': mode,
        'stream_url': f'/api/oauth/graph-extract-token/{task_id}/stream',
    })


@app.route('/api/oauth/graph-extract-token/<task_id>/stream', methods=['GET'])
@login_required
def api_graph_extract_token_stream(task_id: str):
    task = GRAPH_OAUTH_TASKS.pop(task_id, None)
    if not task:
        return Response(
            graph_oauth_sse({'type': 'error', 'success': False, 'message': '授权任务不存在或已过期'})
            + graph_oauth_sse({'type': 'complete', 'success': False}),
            mimetype='text/event-stream',
        )

    def generate():
        output_queue: "queue.Queue[Dict[str, Any] | object]" = queue.Queue()
        worker = threading.Thread(
            target=run_graph_oauth_task,
            args=(int(task['account_id']), output_queue, normalize_graph_oauth_mode(task.get('mode'))),
            name=f"graph-oauth-{task_id[:8]}",
            daemon=True,
        )
        worker.start()

        while True:
            payload = output_queue.get()
            if payload is GRAPH_OAUTH_DONE:
                break
            yield graph_oauth_sse(payload)
        worker.join(timeout=1)

    return Response(stream_with_context(generate()), mimetype='text/event-stream')
