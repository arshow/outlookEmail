"""独立收件箱发现：定时抓信 upsert 本地保留 + SSE 推送。"""

import json
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from flask import Response, jsonify, request, stream_with_context


INBOX_POLL_INTERVAL_SECONDS_MIN = 20
INBOX_POLL_INTERVAL_SECONDS_MAX = 3600
INBOX_POLL_INTERVAL_SECONDS_DEFAULT = 120
INBOX_POLL_ACCOUNT_DELAY_SECONDS_DEFAULT = 1
INBOX_POLL_CONCURRENCY_DEFAULT = 5
INBOX_POLL_CONCURRENCY_MIN = 1
INBOX_POLL_CONCURRENCY_MAX = 20
INBOX_POLL_TOP_DEFAULT = 20
INBOX_POLL_TOP_MAX = 50
INBOX_DISCOVERY_SSE_HEARTBEAT_SECONDS = 20
INBOX_DISCOVERY_SSE_QUEUE_MAXSIZE = 64

inbox_discovery_run_lock = threading.Lock()
_inbox_discovery_subscribers_lock = threading.Lock()
_inbox_discovery_subscribers: Dict[str, queue.Queue] = {}


def normalize_inbox_poll_interval_seconds(value: Any = None) -> int:
    raw = (
        get_setting('inbox_poll_interval_seconds', str(INBOX_POLL_INTERVAL_SECONDS_DEFAULT))
        if value is None
        else value
    )
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        seconds = INBOX_POLL_INTERVAL_SECONDS_DEFAULT
    return max(INBOX_POLL_INTERVAL_SECONDS_MIN, min(INBOX_POLL_INTERVAL_SECONDS_MAX, seconds))


def parse_inbox_poll_interval_seconds_input(value: Any) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise ValueError(
            f'收件箱发现轮询间隔必须在 {INBOX_POLL_INTERVAL_SECONDS_MIN}-'
            f'{INBOX_POLL_INTERVAL_SECONDS_MAX} 秒之间'
        )
    if seconds < INBOX_POLL_INTERVAL_SECONDS_MIN or seconds > INBOX_POLL_INTERVAL_SECONDS_MAX:
        raise ValueError(
            f'收件箱发现轮询间隔必须在 {INBOX_POLL_INTERVAL_SECONDS_MIN}-'
            f'{INBOX_POLL_INTERVAL_SECONDS_MAX} 秒之间'
        )
    return seconds


def normalize_inbox_poll_account_delay_seconds(value: Any = None) -> int:
    raw = (
        get_setting('inbox_poll_account_delay_seconds', str(INBOX_POLL_ACCOUNT_DELAY_SECONDS_DEFAULT))
        if value is None
        else value
    )
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        seconds = INBOX_POLL_ACCOUNT_DELAY_SECONDS_DEFAULT
    return max(0, min(60, seconds))


def parse_inbox_poll_account_delay_seconds_input(value: Any) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise ValueError('收件箱发现账号间隔必须是数字')
    if seconds < 0 or seconds > 60:
        raise ValueError('收件箱发现账号间隔必须在 0-60 秒之间')
    return seconds


def normalize_inbox_poll_concurrency(value: Any = None) -> int:
    raw = (
        get_setting('inbox_poll_concurrency', str(INBOX_POLL_CONCURRENCY_DEFAULT))
        if value is None
        else value
    )
    try:
        workers = int(raw)
    except (TypeError, ValueError):
        workers = INBOX_POLL_CONCURRENCY_DEFAULT
    return max(INBOX_POLL_CONCURRENCY_MIN, min(INBOX_POLL_CONCURRENCY_MAX, workers))


def parse_inbox_poll_concurrency_input(value: Any) -> int:
    try:
        workers = int(value)
    except (TypeError, ValueError):
        raise ValueError(
            f'收件箱发现并发数必须在 {INBOX_POLL_CONCURRENCY_MIN}-'
            f'{INBOX_POLL_CONCURRENCY_MAX} 之间'
        )
    if workers < INBOX_POLL_CONCURRENCY_MIN or workers > INBOX_POLL_CONCURRENCY_MAX:
        raise ValueError(
            f'收件箱发现并发数必须在 {INBOX_POLL_CONCURRENCY_MIN}-'
            f'{INBOX_POLL_CONCURRENCY_MAX} 之间'
        )
    return workers


def normalize_inbox_poll_top(value: Any = None) -> int:
    raw = get_setting('inbox_poll_top', str(INBOX_POLL_TOP_DEFAULT)) if value is None else value
    try:
        top = int(raw)
    except (TypeError, ValueError):
        top = INBOX_POLL_TOP_DEFAULT
    return max(1, min(INBOX_POLL_TOP_MAX, top))


def is_inbox_poll_scheduler_enabled() -> bool:
    return str(get_setting('inbox_poll_scheduler_enabled', 'true')).strip().lower() in {
        '1', 'true', 'yes', 'on'
    }


def build_inbox_discovery_poll_trigger(interval_trigger_cls, interval_seconds: Any, timezone):
    seconds = normalize_inbox_poll_interval_seconds(interval_seconds)
    return interval_trigger_cls(seconds=seconds, timezone=timezone)


def reschedule_inbox_discovery_job() -> bool:
    """热更新 inbox_discovery IntervalTrigger；调度器未启动时返回 False。"""
    global scheduler_instance
    with scheduler_lock:
        scheduler = scheduler_instance
        if scheduler is None:
            return False
        if not is_inbox_poll_scheduler_enabled():
            try:
                scheduler.remove_job('inbox_discovery')
            except Exception:
                pass
            return True
        try:
            from apscheduler.triggers.interval import IntervalTrigger
            from zoneinfo import ZoneInfo

            app_timezone = get_setting('app_timezone', 'Asia/Shanghai') or 'Asia/Shanghai'
            try:
                app_tzinfo = ZoneInfo(app_timezone)
            except Exception:
                app_tzinfo = ZoneInfo('Asia/Shanghai')
            interval_seconds = normalize_inbox_poll_interval_seconds()
            scheduler.add_job(
                func=process_inbox_discovery_job,
                trigger=build_inbox_discovery_poll_trigger(IntervalTrigger, interval_seconds, app_tzinfo),
                id='inbox_discovery',
                name='收件箱发现轮询',
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            return True
        except Exception as exc:
            safe_console_print(f'[inbox-discovery] reschedule failed: {exc}')
            return False


def register_inbox_discovery_subscriber() -> tuple[str, queue.Queue]:
    subscriber_id = uuid.uuid4().hex
    event_queue: queue.Queue = queue.Queue(maxsize=INBOX_DISCOVERY_SSE_QUEUE_MAXSIZE)
    with _inbox_discovery_subscribers_lock:
        _inbox_discovery_subscribers[subscriber_id] = event_queue
    return subscriber_id, event_queue


def unregister_inbox_discovery_subscriber(subscriber_id: str) -> None:
    with _inbox_discovery_subscribers_lock:
        _inbox_discovery_subscribers.pop(subscriber_id, None)


def publish_inbox_discovery_event(event: Dict[str, Any]) -> int:
    payload = dict(event or {})
    if 'type' not in payload:
        payload['type'] = 'new_mail'
    delivered = 0
    with _inbox_discovery_subscribers_lock:
        subscribers = list(_inbox_discovery_subscribers.items())
    for subscriber_id, event_queue in subscribers:
        try:
            event_queue.put_nowait(payload)
            delivered += 1
        except queue.Full:
            try:
                event_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                event_queue.put_nowait(payload)
                delivered += 1
            except queue.Full:
                unregister_inbox_discovery_subscriber(subscriber_id)
    return delivered


def get_inbox_poll_enabled_account_ids() -> List[int]:
    db = get_db()
    rows = db.execute(
        '''
        SELECT id
        FROM accounts
        WHERE status = 'active'
          AND COALESCE(inbox_poll_enabled, 1) = 1
        ORDER BY id ASC
        '''
    ).fetchall()
    return [int(row['id']) for row in rows]


def set_account_inbox_poll_last_checked_at(account_id: int, checked_at: Optional[str] = None) -> bool:
    db = get_db()
    try:
        cursor_value = checked_at or datetime.now(timezone.utc).isoformat()
        db.execute(
            '''
            UPDATE accounts
            SET inbox_poll_last_checked_at = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            ''',
            (cursor_value, account_id)
        )
        db.commit()
        return True
    except Exception:
        db.rollback()
        return False


def discover_account_inbox_mail(account: Dict[str, Any], top: int) -> Dict[str, Any]:
    """抓取单账号 inbox 列表，upsert 本地保留，并在有新信时发布 SSE。"""
    account_id = int(account.get('id') or 0)
    account_email = str(account.get('email') or '')
    result: Dict[str, Any] = {
        'success': False,
        'account_id': account_id,
        'account_email': account_email,
        'new_count': 0,
        'new_message_ids': [],
        'emails': [],
        'error': '',
    }
    if not account_id or not account_email:
        result['error'] = '账号无效'
        return result

    proxy_url = get_account_proxy_url(account)
    fallback_proxy_urls = get_account_proxy_failover_urls(account)
    fetch_result = fetch_account_folder_emails(
        account,
        'inbox',
        0,
        top,
        proxy_url=proxy_url,
        fallback_proxy_urls=fallback_proxy_urls,
    )
    if not fetch_result.get('success'):
        result['error'] = fetch_result.get('error') or '抓取收件箱失败'
        return result

    emails = list(fetch_result.get('emails') or [])
    new_message_ids = find_new_retained_normal_mail_identifiers(account, 'inbox', emails)
    upsert_retained_normal_mail_list_items(account, 'inbox', emails)
    set_account_inbox_poll_last_checked_at(account_id)

    new_id_set = {
        str(item.get('id') or item.get('message_id') or '').strip()
        for item in new_message_ids
        if str(item.get('id') or item.get('message_id') or '').strip()
    }
    new_emails = []
    for email in emails:
        if str(email.get('id') or '').strip() not in new_id_set:
            continue
        annotated = dict(email)
        annotated['account_id'] = account_id
        annotated['account_email'] = account_email
        new_emails.append(annotated)

    result.update({
        'success': True,
        'new_count': len(new_message_ids),
        'new_message_ids': new_message_ids,
        'emails': new_emails,
        'fetched_count': len(emails),
    })

    if new_message_ids:
        publish_inbox_discovery_event({
            'type': 'new_mail',
            'account_id': account_id,
            'account_email': account_email,
            'folder': 'inbox',
            'new_count': len(new_message_ids),
            'new_message_ids': new_message_ids,
            'emails': new_emails,
        })
    return result


def process_inbox_discovery_job() -> Dict[str, Any]:
    """APScheduler / 手动触发：扫描开启自动发现的账号。"""
    if not inbox_discovery_run_lock.acquire(blocking=False):
        return {
            'success': False,
            'skipped': True,
            'reason': 'already_running',
        }

    try:
        with app.app_context():
            if not is_inbox_poll_scheduler_enabled():
                return {
                    'success': True,
                    'skipped': True,
                    'reason': 'scheduler_disabled',
                    'accounts_total': 0,
                    'accounts_processed': 0,
                    'accounts_with_new_mail': 0,
                    'new_mail_total': 0,
                    'failed_count': 0,
                }

            if not is_normal_mail_local_retention_enabled():
                return {
                    'success': True,
                    'skipped': True,
                    'reason': 'local_retention_disabled',
                    'accounts_total': 0,
                    'accounts_processed': 0,
                    'accounts_with_new_mail': 0,
                    'new_mail_total': 0,
                    'failed_count': 0,
                }

            account_ids = get_inbox_poll_enabled_account_ids()
            top = normalize_inbox_poll_top()
            delay_seconds = normalize_inbox_poll_account_delay_seconds()
            concurrency = normalize_inbox_poll_concurrency()
            accounts_processed = 0
            accounts_with_new_mail = 0
            new_mail_total = 0
            failed_count = 0
            account_errors: List[Dict[str, Any]] = []

            def _discover_one(account_id: int) -> Dict[str, Any]:
                with app.app_context():
                    account = get_account_by_id(account_id)
                    if not account:
                        return {
                            'account_id': account_id,
                            'account_email': '',
                            'success': False,
                            'missing': True,
                            'error': '账号不存在',
                        }
                    try:
                        discover_result = discover_account_inbox_mail(account, top)
                        return {
                            'account_id': account_id,
                            'account_email': account.get('email', ''),
                            'success': bool(discover_result.get('success')),
                            'new_count': int(discover_result.get('new_count') or 0),
                            'error': discover_result.get('error') or '',
                        }
                    except Exception as exc:
                        safe_console_print(
                            f'[inbox-discovery] account failed: id={account_id} error={exc}'
                        )
                        return {
                            'account_id': account_id,
                            'account_email': account.get('email', ''),
                            'success': False,
                            'new_count': 0,
                            'error': str(exc),
                        }

            def _accumulate(item: Dict[str, Any]) -> None:
                nonlocal accounts_processed, accounts_with_new_mail, new_mail_total, failed_count
                if item.get('missing'):
                    failed_count += 1
                    return
                accounts_processed += 1
                if not item.get('success'):
                    failed_count += 1
                    account_errors.append({
                        'account_id': item.get('account_id'),
                        'account_email': item.get('account_email', ''),
                        'error': item.get('error') or '发现失败',
                    })
                    return
                new_count = int(item.get('new_count') or 0)
                new_mail_total += new_count
                if new_count > 0:
                    accounts_with_new_mail += 1

            if concurrency <= 1 or len(account_ids) <= 1:
                for index, account_id in enumerate(account_ids):
                    _accumulate(_discover_one(account_id))
                    if delay_seconds > 0 and index < len(account_ids) - 1:
                        time.sleep(delay_seconds)
            else:
                workers = min(concurrency, len(account_ids))
                with ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix='inbox-discovery',
                ) as executor:
                    future_map = {}
                    for index, account_id in enumerate(account_ids):
                        future_map[executor.submit(_discover_one, account_id)] = account_id
                        # 提交错峰，减轻瞬时打满 Graph/IMAP
                        if delay_seconds > 0 and index < len(account_ids) - 1:
                            time.sleep(delay_seconds)
                    for future in as_completed(future_map):
                        try:
                            _accumulate(future.result())
                        except Exception as exc:
                            account_id = future_map.get(future)
                            failed_count += 1
                            account_errors.append({
                                'account_id': account_id,
                                'account_email': '',
                                'error': str(exc),
                            })

            summary = {
                'success': True,
                'skipped': False,
                'accounts_total': len(account_ids),
                'accounts_processed': accounts_processed,
                'accounts_with_new_mail': accounts_with_new_mail,
                'new_mail_total': new_mail_total,
                'failed_count': failed_count,
                'concurrency': concurrency,
                'errors': account_errors[:20],
            }
            safe_console_print(
                '[inbox-discovery] job done: '
                f"total={summary['accounts_total']} "
                f"processed={summary['accounts_processed']} "
                f"with_new={summary['accounts_with_new_mail']} "
                f"new_mail={summary['new_mail_total']} "
                f"failed={summary['failed_count']} "
                f"concurrency={summary['concurrency']}"
            )
            return summary
    finally:
        inbox_discovery_run_lock.release()


@app.route('/api/accounts/trigger-inbox-discovery', methods=['POST'])
@login_required
def api_trigger_inbox_discovery():
    """手动触发一次收件箱发现。"""
    try:
        result = process_inbox_discovery_job()
        if result.get('reason') == 'already_running':
            return jsonify({'success': False, 'error': '收件箱发现正在执行中，请稍后再试'}), 409
        if result.get('reason') == 'local_retention_disabled':
            return jsonify({
                **result,
                'success': False,
                'error': '请先开启普通邮箱本地保留，收件箱发现才会抓信并缓存',
            }), 400
        if result.get('reason') == 'scheduler_disabled':
            return jsonify({
                **result,
                'success': False,
                'error': '收件箱发现总开关已关闭',
            }), 400
        return jsonify({
            **result,
            'success': True,
            'message': (
                f"已完成一次收件箱发现：处理 {result.get('accounts_processed', 0)} 个账号，"
                f"发现新邮件 {result.get('new_mail_total', 0)} 封"
            ),
        })
    except Exception as exc:
        return jsonify({'success': False, 'error': f'触发收件箱发现失败: {str(exc)}'})


@app.route('/api/emails/inbox-discovery/events', methods=['GET'])
@login_required
def api_inbox_discovery_events():
    """长连接 SSE：订阅收件箱发现新邮件事件（进程内广播，需单 worker）。"""
    subscriber_id, event_queue = register_inbox_discovery_subscriber()

    def event_stream():
        try:
            yield f"data: {json.dumps({'type': 'connected', 'subscriber_id': subscriber_id})}\n\n"
            while True:
                try:
                    event = event_queue.get(timeout=INBOX_DISCOVERY_SSE_HEARTBEAT_SECONDS)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield f"data: {json.dumps({'type': 'heartbeat', 'ts': int(time.time())})}\n\n"
        except GeneratorExit:
            pass
        finally:
            unregister_inbox_discovery_subscriber(subscriber_id)

    response = Response(
        stream_with_context(event_stream()),
        mimetype='text/event-stream',
    )
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Connection'] = 'keep-alive'
    return response


# 所有依赖段加载完成后启动调度器（含 inbox_discovery）
ensure_scheduler_started()
