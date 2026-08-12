from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from outlook_web.ai.constants import CONTEXT_SCOPE_CURRENT, CONTEXT_SCOPES, PROVIDERS
from outlook_web.ai.db import (
    bump_knowledge_revision,
    ensure_ai_schema,
    list_knowledge_entries,
    list_published_rules,
    list_rule_versions,
)
from outlook_web.ai.knowledge import parse_keywords
from outlook_web.ai.llm import test_provider_connection
from outlook_web.ai.rules import match_rules, parse_forbidden_phrases, preclassify
from outlook_web.ai.service import analyze_email, refine_reply, translate_email_to_zh
from outlook_web.ai.settings import (
    get_ai_reply_settings,
    public_ai_reply_settings,
    save_ai_reply_settings,
)


def _ensure_ai_tables():
    db = get_db()
    ensure_ai_schema(db)
    db.commit()


def _load_ai_settings() -> Dict[str, Any]:
    return get_ai_reply_settings(
        get_setting=get_setting,
        get_setting_decrypted=get_setting_decrypted,
    )


def _json_list_field(value: Any) -> str:
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        return json.dumps(items, ensure_ascii=False)
    text = str(value or '').strip()
    if not text:
        return '[]'
    if text.startswith('['):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return json.dumps([str(item).strip() for item in parsed if str(item).strip()], ensure_ascii=False)
        except Exception:
            pass
    items = [part.strip() for part in text.replace('，', ',').split(',') if part.strip()]
    return json.dumps(items, ensure_ascii=False)


def _serialize_knowledge(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'id': row.get('id'),
        'category': row.get('category') or 'general',
        'title': row.get('title') or '',
        'content': row.get('content') or '',
        'keywords': parse_keywords(row.get('keywords')),
        'priority': int(row.get('priority') or 0),
        'enabled': bool(row.get('enabled')),
        'created_at': row.get('created_at'),
        'updated_at': row.get('updated_at'),
    }


def _serialize_rule(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'id': row.get('id'),
        'version_label': row.get('version_label') or '',
        'status': row.get('status') or 'draft',
        'keywords': parse_keywords(row.get('keywords')),
        'intents': parse_keywords(row.get('intents')),
        'instruction': row.get('instruction') or '',
        'forbidden_phrases': parse_forbidden_phrases(row.get('forbidden_phrases')),
        'risk_level': row.get('risk_level') or 'yellow',
        'priority': int(row.get('priority') or 0),
        'enabled': bool(row.get('enabled')),
        'created_at': row.get('created_at'),
        'updated_at': row.get('updated_at'),
        'published_at': row.get('published_at'),
    }


@app.route('/ai')
@login_required
def ai_admin_page():
    _ensure_ai_tables()
    return render_template('ai_admin.html')


@app.route('/api/ai/settings', methods=['GET'])
@login_required
def api_ai_settings_get():
    _ensure_ai_tables()
    settings = public_ai_reply_settings(_load_ai_settings())
    return jsonify({'success': True, 'settings': settings})


@app.route('/api/ai/settings', methods=['PUT'])
@login_required
def api_ai_settings_put():
    _ensure_ai_tables()
    data = request.get_json(silent=True) or {}
    try:
        settings = save_ai_reply_settings(
            data,
            get_setting=get_setting,
            get_setting_decrypted=get_setting_decrypted,
            set_setting=set_setting,
            set_setting_encrypted=set_setting_encrypted,
        )
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    return jsonify({'success': True, 'settings': public_ai_reply_settings(settings)})


@app.route('/api/ai/settings/test', methods=['POST'])
@login_required
def api_ai_settings_test():
    _ensure_ai_tables()
    data = request.get_json(silent=True) or {}
    settings = _load_ai_settings()
    provider = str(data.get('provider') or settings.get('provider') or '').strip().lower()
    if provider and provider not in PROVIDERS:
        return jsonify({'success': False, 'error': '无效的提供商'}), 400
    # Allow temporary overrides for testing without saving.
    if data.get('model'):
        settings['model'] = str(data.get('model')).strip()
    if data.get('gemini_api_key'):
        settings['gemini_api_key'] = str(data.get('gemini_api_key')).strip()
    if data.get('deepseek_api_key'):
        settings['deepseek_api_key'] = str(data.get('deepseek_api_key')).strip()
    if data.get('gemini_base_url') is not None:
        settings['gemini_base_url'] = str(data.get('gemini_base_url') or '').strip()
    if data.get('deepseek_base_url') is not None:
        settings['deepseek_base_url'] = str(data.get('deepseek_base_url') or '').strip()
    if isinstance(data.get('gemini_socks5'), dict):
        from outlook_web.ai.settings import parse_socks5
        socks = parse_socks5(data.get('gemini_socks5'))
        current = settings.get('gemini_socks5_full') or {}
        if not socks.get('password') and current.get('password'):
            socks['password'] = current['password']
        settings['gemini_socks5_full'] = socks
        settings['gemini_socks5'] = socks
    try:
        result = test_provider_connection(settings, provider=provider or None)
        return jsonify(result)
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400


@app.route('/api/ai/knowledge', methods=['GET'])
@login_required
def api_ai_knowledge_list():
    _ensure_ai_tables()
    db = get_db()
    entries = [_serialize_knowledge(row) for row in list_knowledge_entries(db, include_disabled=True)]
    return jsonify({'success': True, 'entries': entries})


@app.route('/api/ai/knowledge', methods=['POST'])
@login_required
def api_ai_knowledge_create():
    _ensure_ai_tables()
    data = request.get_json(silent=True) or {}
    title = str(data.get('title') or '').strip()
    content = str(data.get('content') or '').strip()
    if not title or not content:
        return jsonify({'success': False, 'error': '标题和内容不能为空'}), 400
    db = get_db()
    cursor = db.execute(
        '''
        INSERT INTO ai_knowledge_entries (category, title, content, keywords, priority, enabled)
        VALUES (?, ?, ?, ?, ?, ?)
        ''',
        (
            str(data.get('category') or 'general').strip() or 'general',
            title,
            content,
            _json_list_field(data.get('keywords')),
            int(data.get('priority') or 0),
            1 if data.get('enabled', True) else 0,
        ),
    )
    bump_knowledge_revision(db)
    db.commit()
    row = db.execute('SELECT * FROM ai_knowledge_entries WHERE id = ?', (cursor.lastrowid,)).fetchone()
    return jsonify({'success': True, 'entry': _serialize_knowledge(dict(row))})


@app.route('/api/ai/knowledge/<int:entry_id>', methods=['PUT'])
@login_required
def api_ai_knowledge_update(entry_id: int):
    _ensure_ai_tables()
    data = request.get_json(silent=True) or {}
    db = get_db()
    existing = db.execute('SELECT * FROM ai_knowledge_entries WHERE id = ?', (entry_id,)).fetchone()
    if not existing:
        return jsonify({'success': False, 'error': '知识条目不存在'}), 404
    title = str(data.get('title', existing['title']) or '').strip()
    content = str(data.get('content', existing['content']) or '').strip()
    if not title or not content:
        return jsonify({'success': False, 'error': '标题和内容不能为空'}), 400
    db.execute(
        '''
        UPDATE ai_knowledge_entries
        SET category = ?, title = ?, content = ?, keywords = ?, priority = ?, enabled = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        ''',
        (
            str(data.get('category', existing['category']) or 'general').strip() or 'general',
            title,
            content,
            _json_list_field(data['keywords'] if 'keywords' in data else existing['keywords']),
            int(data.get('priority', existing['priority']) or 0),
            1 if data.get('enabled', bool(existing['enabled'])) else 0,
            entry_id,
        ),
    )
    bump_knowledge_revision(db)
    db.commit()
    row = db.execute('SELECT * FROM ai_knowledge_entries WHERE id = ?', (entry_id,)).fetchone()
    return jsonify({'success': True, 'entry': _serialize_knowledge(dict(row))})


@app.route('/api/ai/knowledge/<int:entry_id>', methods=['DELETE'])
@login_required
def api_ai_knowledge_delete(entry_id: int):
    _ensure_ai_tables()
    db = get_db()
    existing = db.execute('SELECT id FROM ai_knowledge_entries WHERE id = ?', (entry_id,)).fetchone()
    if not existing:
        return jsonify({'success': False, 'error': '知识条目不存在'}), 404
    db.execute('DELETE FROM ai_knowledge_entries WHERE id = ?', (entry_id,))
    bump_knowledge_revision(db)
    db.commit()
    return jsonify({'success': True})


@app.route('/api/ai/rules', methods=['GET'])
@login_required
def api_ai_rules_list():
    _ensure_ai_tables()
    db = get_db()
    return jsonify({
        'success': True,
        'rules': [_serialize_rule(row) for row in list_rule_versions(db)],
        'published': [_serialize_rule(row) for row in list_published_rules(db)],
    })


@app.route('/api/ai/rules', methods=['POST'])
@login_required
def api_ai_rules_create():
    _ensure_ai_tables()
    data = request.get_json(silent=True) or {}
    instruction = str(data.get('instruction') or '').strip()
    if not instruction:
        return jsonify({'success': False, 'error': '规则 instruction 不能为空'}), 400
    risk_level = str(data.get('risk_level') or 'yellow').strip().lower()
    if risk_level not in ('green', 'yellow', 'red'):
        risk_level = 'yellow'
    status = str(data.get('status') or 'draft').strip().lower()
    if status not in ('draft', 'published'):
        status = 'draft'
    db = get_db()
    if status == 'published':
        # Keep multiple published rules allowed (policy pack). No mass unpublish.
        pass
    cursor = db.execute(
        '''
        INSERT INTO ai_rule_versions (
            version_label, status, keywords, intents, instruction,
            forbidden_phrases, risk_level, priority, enabled, published_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? = 'published' THEN CURRENT_TIMESTAMP ELSE NULL END)
        ''',
        (
            str(data.get('version_label') or '').strip(),
            status,
            _json_list_field(data.get('keywords')),
            _json_list_field(data.get('intents')),
            instruction,
            _json_list_field(data.get('forbidden_phrases')),
            risk_level,
            int(data.get('priority') or 0),
            1 if data.get('enabled', True) else 0,
            status,
        ),
    )
    db.commit()
    row = db.execute('SELECT * FROM ai_rule_versions WHERE id = ?', (cursor.lastrowid,)).fetchone()
    return jsonify({'success': True, 'rule': _serialize_rule(dict(row))})


@app.route('/api/ai/rules/<int:rule_id>', methods=['PUT'])
@login_required
def api_ai_rules_update(rule_id: int):
    _ensure_ai_tables()
    data = request.get_json(silent=True) or {}
    db = get_db()
    existing = db.execute('SELECT * FROM ai_rule_versions WHERE id = ?', (rule_id,)).fetchone()
    if not existing:
        return jsonify({'success': False, 'error': '规则不存在'}), 404
    instruction = str(data.get('instruction', existing['instruction']) or '').strip()
    if not instruction:
        return jsonify({'success': False, 'error': '规则 instruction 不能为空'}), 400
    risk_level = str(data.get('risk_level', existing['risk_level']) or 'yellow').strip().lower()
    if risk_level not in ('green', 'yellow', 'red'):
        risk_level = 'yellow'
    status = str(data.get('status', existing['status']) or 'draft').strip().lower()
    if status not in ('draft', 'published', 'archived'):
        status = existing['status']
    db.execute(
        '''
        UPDATE ai_rule_versions
        SET version_label = ?, status = ?, keywords = ?, intents = ?, instruction = ?,
            forbidden_phrases = ?, risk_level = ?, priority = ?, enabled = ?,
            published_at = CASE
                WHEN ? = 'published' AND COALESCE(published_at, '') = '' THEN CURRENT_TIMESTAMP
                WHEN ? = 'published' THEN published_at
                ELSE published_at
            END,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        ''',
        (
            str(data.get('version_label', existing['version_label']) or '').strip(),
            status,
            _json_list_field(data['keywords'] if 'keywords' in data else existing['keywords']),
            _json_list_field(data['intents'] if 'intents' in data else existing['intents']),
            instruction,
            _json_list_field(data['forbidden_phrases'] if 'forbidden_phrases' in data else existing['forbidden_phrases']),
            risk_level,
            int(data.get('priority', existing['priority']) or 0),
            1 if data.get('enabled', bool(existing['enabled'])) else 0,
            status,
            status,
            rule_id,
        ),
    )
    db.commit()
    row = db.execute('SELECT * FROM ai_rule_versions WHERE id = ?', (rule_id,)).fetchone()
    return jsonify({'success': True, 'rule': _serialize_rule(dict(row))})


@app.route('/api/ai/rules/<int:rule_id>/publish', methods=['POST'])
@login_required
def api_ai_rules_publish(rule_id: int):
    _ensure_ai_tables()
    db = get_db()
    existing = db.execute('SELECT id FROM ai_rule_versions WHERE id = ?', (rule_id,)).fetchone()
    if not existing:
        return jsonify({'success': False, 'error': '规则不存在'}), 404
    db.execute(
        '''
        UPDATE ai_rule_versions
        SET status = 'published', published_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        ''',
        (rule_id,),
    )
    db.commit()
    row = db.execute('SELECT * FROM ai_rule_versions WHERE id = ?', (rule_id,)).fetchone()
    return jsonify({'success': True, 'rule': _serialize_rule(dict(row))})


@app.route('/api/ai/rules/<int:rule_id>', methods=['DELETE'])
@login_required
def api_ai_rules_delete(rule_id: int):
    _ensure_ai_tables()
    db = get_db()
    existing = db.execute('SELECT id FROM ai_rule_versions WHERE id = ?', (rule_id,)).fetchone()
    if not existing:
        return jsonify({'success': False, 'error': '规则不存在'}), 404
    db.execute('DELETE FROM ai_rule_versions WHERE id = ?', (rule_id,))
    db.commit()
    return jsonify({'success': True})


@app.route('/api/ai/rules/test', methods=['POST'])
@login_required
def api_ai_rules_test():
    _ensure_ai_tables()
    data = request.get_json(silent=True) or {}
    text = str(data.get('text') or '').strip()
    if not text:
        return jsonify({'success': False, 'error': '请提供测试文本'}), 400
    db = get_db()
    published = list_published_rules(db)
    matched = match_rules(published, text)
    return jsonify({
        'success': True,
        'preclassify': preclassify(text),
        'matched_rules': [_serialize_rule(rule) for rule in matched],
    })


def _ai_detail_has_usable_body(detail: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(detail, dict):
        return False
    body = detail.get('body')
    if isinstance(body, dict):
        body = body.get('content')
    text = str(body or detail.get('body_preview') or detail.get('bodyPreview') or '').strip()
    return bool(text)


def _normalize_client_email_detail(raw: Any, message_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    detail = dict(raw)
    if not detail.get('id'):
        detail['id'] = message_id
    if not detail.get('body') and (detail.get('body_preview') or detail.get('bodyPreview')):
        detail['body'] = detail.get('body_preview') or detail.get('bodyPreview')
        detail.setdefault('body_type', 'text')
    if not _ai_detail_has_usable_body(detail) and not str(detail.get('subject') or '').strip():
        return None
    return detail


def _fetch_local_ai_email_detail(account: Dict[str, Any], folder: str,
                                 message_id: str, id_mode: str = '') -> Optional[Dict[str, Any]]:
    """Prefer full local body; fall back to list-row preview for AI context."""
    retained = fetch_retained_normal_mail_detail(account, folder, message_id, id_mode)
    if retained and isinstance(retained.get('email'), dict) and _ai_detail_has_usable_body(retained['email']):
        return retained['email']

    account_id = int((account or {}).get('id') or 0)
    provider_message_id = str(message_id or '').strip()
    if not account_id or not provider_message_id:
        return None

    folder_name = normalize_folder_name(folder)
    requested_id_mode = str(id_mode or '').strip().lower()
    params: List[Any] = [account_id, provider_message_id]
    folder_filter = ''
    id_mode_filter = ''
    if folder_name != 'all':
        folder_filter = 'AND folder = ?'
        params.append(folder_name)
    if requested_id_mode:
        id_mode_filter = 'AND id_mode = ?'
        params.append(requested_id_mode)

    row = get_db().execute(
        f'''
        SELECT provider_message_id, id_mode, folder, subject, sender,
               recipients, cc, received_at, body, body_type, body_preview,
               attachments_json, has_attachments, body_cached
        FROM retained_normal_mail_messages
        WHERE account_id = ?
          AND provider_message_id = ?
          {folder_filter}
          {id_mode_filter}
        ORDER BY body_cached DESC, COALESCE(body_cached_at, updated_at, created_at) DESC, id DESC
        LIMIT 1
        ''',
        params,
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    body = item.get('body') if item.get('body_cached') else ''
    if not str(body or '').strip():
        body = item.get('body_preview') or ''
    if not str(body or '').strip() and not str(item.get('subject') or '').strip():
        return None
    return {
        'id': item.get('provider_message_id') or message_id,
        'subject': item.get('subject') or '无主题',
        'from': item.get('sender') or '未知',
        'to': item.get('recipients') or '',
        'cc': item.get('cc') or '',
        'date': item.get('received_at') or '',
        'received_at': item.get('received_at') or '',
        'body': body or '',
        'body_preview': item.get('body_preview') or '',
        'body_type': item.get('body_type') or 'text',
        'folder': item.get('folder') or folder_name,
        'id_mode': item.get('id_mode') or '',
    }


def resolve_ai_analyze_email_detail(
    account: Dict[str, Any],
    *,
    message_id: str,
    folder: str,
    method: str,
    id_mode: str,
    client_detail: Any = None,
) -> Dict[str, Any]:
    """
    Resolve email content for AI using local sources only.
    Never calls remote IMAP/Graph. Priority: local retention -> opened client detail.
    """
    del method  # AI analyze intentionally ignores remote fetch method.
    local_detail = _fetch_local_ai_email_detail(account, folder, message_id, id_mode)
    if local_detail and _ai_detail_has_usable_body(local_detail):
        return {'success': True, 'email': local_detail, 'source': 'local'}

    normalized_client = _normalize_client_email_detail(client_detail, message_id)
    if normalized_client and _ai_detail_has_usable_body(normalized_client):
        return {'success': True, 'email': normalized_client, 'source': 'client'}

    if local_detail:
        return {'success': True, 'email': local_detail, 'source': 'local_preview'}
    if normalized_client:
        return {'success': True, 'email': normalized_client, 'source': 'client'}

    return {
        'success': False,
        'error': '本地无可用邮件内容。请先打开并加载该邮件，或启用普通邮件本地保留后再试。',
        'code': 'AI_LOCAL_EMAIL_DETAIL_MISSING',
    }


@app.route('/api/ai/analyze', methods=['POST'])
@login_required
def api_ai_analyze():
    _ensure_ai_tables()
    data = request.get_json(silent=True) or {}
    email_addr = str(data.get('email') or '').strip()
    message_id = str(data.get('message_id') or '').strip()
    context_scope = str(data.get('context_scope') or CONTEXT_SCOPE_CURRENT).strip().lower()
    force_refresh = bool(data.get('force_refresh'))
    if not email_addr or not message_id:
        return jsonify({'success': False, 'error': 'email 与 message_id 必填'}), 400
    if context_scope not in CONTEXT_SCOPES:
        return jsonify({'success': False, 'error': 'context_scope 无效'}), 400

    account = get_account_by_email(email_addr)
    if not account:
        return jsonify({'success': False, 'error': '账号不存在'}), 404

    folder = normalize_folder_name(data.get('folder') or 'inbox')
    method = str(data.get('method') or 'graph').strip() or 'graph'
    id_mode = str(data.get('id_mode') or '').strip().lower()
    detail_result = resolve_ai_analyze_email_detail(
        account,
        message_id=message_id,
        folder=folder,
        method=method,
        id_mode=id_mode,
        client_detail=data.get('email_detail') or data.get('emailDetail'),
    )
    if not detail_result.get('success'):
        return jsonify({
            'success': False,
            'error': detail_result.get('error') or '获取邮件详情失败',
            'details': detail_result.get('details'),
        }), 400

    email_detail = detail_result.get('email') or {}
    settings = _load_ai_settings()
    try:
        result = analyze_email(
            db=get_db(),
            settings=settings,
            account_email=account['email'],
            account_id=int(account['id']),
            message_id=message_id,
            current_detail=email_detail,
            context_scope=context_scope,
            force_refresh=force_refresh,
        )
        if detail_result.get('warning'):
            result['warning'] = detail_result['warning']
        result['detail_source'] = detail_result.get('source')
        return jsonify(result)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/api/ai/refine', methods=['POST'])
@login_required
def api_ai_refine():
    _ensure_ai_tables()
    data = request.get_json(silent=True) or {}
    current_text = str(data.get('reply_text') or data.get('current_text') or '').strip()
    mode = str(data.get('mode') or '').strip().lower()
    if not current_text or not mode:
        return jsonify({'success': False, 'error': 'reply_text 与 mode 必填'}), 400
    settings = _load_ai_settings()
    try:
        result = refine_reply(
            settings=settings,
            current_text=current_text,
            mode=mode,
            analysis=data.get('analysis') if isinstance(data.get('analysis'), dict) else {},
            instruction=str(data.get('instruction') or ''),
            target_language=str(data.get('target_language') or ''),
            db=get_db(),
            run_id=int(data['run_id']) if data.get('run_id') else None,
        )
        return jsonify(result)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/api/ai/translate', methods=['POST'])
@login_required
def api_ai_translate_email():
    """Translate inbound email subject/body using configured /ai provider."""
    _ensure_ai_tables()
    data = request.get_json(silent=True) or {}
    text = str(data.get('text') or '')
    html = str(data.get('html') or '')
    subject = str(data.get('subject') or '')

    plain_body = text.strip()
    if not plain_body and html:
        plain_body = html_to_plain_text(html).strip()
    if not plain_body and not subject.strip():
        return jsonify({'success': False, 'error': 'text 与 html 至少提供一项有效正文'}), 400

    settings = _load_ai_settings()
    try:
        result = translate_email_to_zh(
            settings=settings,
            subject=subject,
            body=plain_body,
        )
        return jsonify(result)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/api/ai/latest', methods=['GET'])
@login_required
def api_ai_latest():
    _ensure_ai_tables()
    email_addr = str(request.args.get('email') or '').strip()
    message_id = str(request.args.get('message_id') or '').strip()
    context_scope = str(request.args.get('context_scope') or '').strip().lower()
    if not email_addr or not message_id:
        return jsonify({'success': False, 'error': 'email 与 message_id 必填'}), 400
    db = get_db()
    params: List[Any] = [email_addr, message_id]
    scope_sql = ''
    if context_scope:
        if context_scope not in CONTEXT_SCOPES:
            return jsonify({'success': False, 'error': 'context_scope 无效'}), 400
        scope_sql = ' AND context_scope = ?'
        params.append(context_scope)
    row = db.execute(
        f'''
        SELECT * FROM ai_analysis_runs
        WHERE account_email = ? AND message_id = ? AND status = 'succeeded'{scope_sql}
        ORDER BY id DESC
        LIMIT 1
        ''',
        tuple(params),
    ).fetchone()
    if not row:
        return jsonify({'success': True, 'found': False})
    payload = {}
    if row['result_json']:
        try:
            payload = json.loads(row['result_json'])
        except Exception:
            payload = {}
    return jsonify({
        'success': True,
        'found': True,
        'run_id': row['id'],
        'provider': row['provider'],
        'model': row['model'],
        'context_scope': row['context_scope'],
        'history_count': row['history_count'],
        'created_at': row['created_at'],
        'analysis': payload.get('analysis'),
        'meta': payload.get('meta'),
    })


@app.route('/api/ai/analysis-runs', methods=['GET'])
@login_required
def api_ai_analysis_runs():
    _ensure_ai_tables()
    try:
        limit = max(1, min(int(request.args.get('limit') or 50), 200))
    except (TypeError, ValueError):
        limit = 50
    db = get_db()
    rows = db.execute(
        '''
        SELECT id, account_email, message_id, context_scope, provider, model, status,
               error_message, duration_ms, history_count, contact_email, created_at, result_json
        FROM ai_analysis_runs
        ORDER BY id DESC
        LIMIT ?
        ''',
        (limit,),
    ).fetchall()
    runs = []
    for row in rows:
        item = dict(row)
        risk_level = None
        summary_zh = None
        raw = item.pop('result_json', None)
        if raw:
            try:
                payload = json.loads(raw)
                analysis = payload.get('analysis') or {}
                risk_level = analysis.get('riskLevel')
                summary_zh = analysis.get('summaryZh')
            except Exception:
                pass
        item['risk_level'] = risk_level
        item['summary_zh'] = summary_zh
        runs.append(item)
    return jsonify({'success': True, 'runs': runs})


@app.route('/api/ai/status', methods=['GET'])
@login_required
def api_ai_status():
    """Lightweight status for compose toolbar."""
    _ensure_ai_tables()
    settings = public_ai_reply_settings(_load_ai_settings())
    ready = bool(
        settings.get('enabled')
        and (
            (settings.get('provider') == 'gemini' and settings.get('gemini_api_key_configured'))
            or (settings.get('provider') == 'deepseek' and settings.get('deepseek_api_key_configured'))
        )
    )
    return jsonify({
        'success': True,
        'enabled': bool(settings.get('enabled')),
        'ready': ready,
        'provider': settings.get('provider'),
        'model': settings.get('model'),
    })
