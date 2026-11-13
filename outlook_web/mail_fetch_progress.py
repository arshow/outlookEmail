"""Short-lived, in-process progress for manual mailbox requests."""

import re
import threading
import time


def _has_timeout(value):
    if isinstance(value, dict):
        if value.get('code') == 'EMAIL_FETCH_TIMEOUT' or value.get('reason_code') == 'MAIL_NETWORK_TIMEOUT':
            return True
        return any(_has_timeout(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_timeout(item) for item in value)
    return False


class MailFetchProgress:
    def __init__(self, labels):
        self.started_at = time.monotonic()
        self.updated_at = self.started_at
        self.lock = threading.Lock()
        self.state = 'running'
        self.units = {str(label): {'state': 'pending', 'fetched': 0} for label in labels}

    def start_unit(self, label):
        with self.lock:
            unit = self.units.get(str(label))
            if self.state == 'running' and unit and unit['state'] == 'pending':
                unit['state'] = 'running'
                self.updated_at = time.monotonic()

    def finish_unit(self, label, result):
        with self.lock:
            unit = self.units.get(str(label))
            if self.state != 'running' or not unit or unit['state'] not in {'pending', 'running'}:
                return
            if result.get('success'):
                unit['state'] = 'partial' if result.get('partial') else 'success'
            else:
                unit['state'] = 'timeout' if _has_timeout(result) else 'failed'
            unit['fetched'] = len(result.get('emails') or [])
            unit['timed_out'] = _has_timeout(result)
            self.updated_at = time.monotonic()

    def finish(self, result):
        with self.lock:
            if self.state != 'running':
                return
            for unit in self.units.values():
                if unit['state'] in {'pending', 'running'}:
                    unit['state'] = 'timeout' if _has_timeout(result) else 'failed'
                    unit['timed_out'] = _has_timeout(result)
            self.state = 'completed' if result.get('success') else 'failed'
            if result.get('partial'):
                self.state = 'partial'
            self.updated_at = time.monotonic()

    def snapshot(self):
        with self.lock:
            units = list(self.units.values())
            return {
                'state': self.state,
                'total': len(units),
                'completed': sum(unit['state'] not in {'pending', 'running'} for unit in units),
                'successful': sum(unit['state'] == 'success' for unit in units),
                'failed': sum(unit['state'] in {'failed', 'timeout'} for unit in units),
                'partial': sum(unit['state'] == 'partial' for unit in units),
                'timed_out': sum(bool(unit.get('timed_out')) for unit in units),
                'fetched': sum(unit['fetched'] for unit in units),
                'active': [label for label, unit in self.units.items() if unit['state'] == 'running'][:3],
                'elapsed_seconds': round((time.monotonic() if self.state == 'running' else self.updated_at) - self.started_at, 1),
            }


_progress_lock = threading.Lock()
_progress_requests = {}


def begin_mail_fetch_progress(request_id, labels):
    if not re.fullmatch(r'[a-f0-9]{32}', str(request_id or '')):
        return None
    now = time.monotonic()
    with _progress_lock:
        for key, progress in list(_progress_requests.items()):
            if now - progress.updated_at > 1800:
                del _progress_requests[key]
        if request_id in _progress_requests or len(_progress_requests) >= 128:
            return None
        progress = MailFetchProgress(labels)
        _progress_requests[request_id] = progress
        return progress


def get_mail_fetch_progress(request_id):
    with _progress_lock:
        progress = _progress_requests.get(request_id)
    if progress is None or time.monotonic() - progress.updated_at > 1800:
        return None
    return progress.snapshot()


_async_lock = threading.Lock()
_async_jobs = {}


def start_async_mail_fetch(request_id, runner):
    """Execute a read request without keeping its HTTP connection open."""
    if not re.fullmatch(r'[a-f0-9]{32}', str(request_id or '')):
        raise ValueError('无效的邮件任务标识')
    now = time.monotonic()
    with _async_lock:
        for key, job in list(_async_jobs.items()):
            if now - job['updated_at'] > 900 and not job['pending']:
                del _async_jobs[key]
        if request_id in _async_jobs:
            return
        if sum(job['pending'] for job in _async_jobs.values()) >= 8:
            raise RuntimeError('正在获取邮件的任务较多，请稍后重试')
        completed = [key for key, job in _async_jobs.items() if not job['pending']]
        while len(_async_jobs) >= 32 and completed:
            del _async_jobs[completed.pop(0)]
        job = {'pending': True, 'result': None, 'updated_at': now}
        _async_jobs[request_id] = job

    def work():
        try:
            result = runner()
        except Exception as exc:
            result = {'success': False, 'error': {
                'code': 'EMAIL_FETCH_FAILED', 'message': '邮件任务执行失败，请重试',
                'type': type(exc).__name__, 'status': 500,
            }}
        with _async_lock:
            job.update(pending=False, result=result, updated_at=time.monotonic())

    try:
        threading.Thread(target=work, name='manual-mail-fetch', daemon=True).start()
    except Exception:
        with _async_lock:
            _async_jobs.pop(request_id, None)
        raise


def get_async_mail_fetch_result(request_id):
    with _async_lock:
        job = _async_jobs.get(request_id)
        if job is None or (not job['pending'] and time.monotonic() - job['updated_at'] > 900):
            return {'pending': False, 'result': {'success': False, 'error': {
                'code': 'MAIL_FETCH_JOB_EXPIRED', 'message': '邮件任务已失效，请重新获取邮件', 'status': 410,
            }}}
        return {'pending': job['pending'], 'result': job['result']}
