import unittest
from unittest.mock import patch

from outlook_web.mail_fetch_progress import (
    MailFetchProgress, begin_mail_fetch_progress, get_mail_fetch_progress,
    start_async_mail_fetch, get_async_mail_fetch_result,
)


class MailFetchProgressTests(unittest.TestCase):
    def test_progress_counts_actual_completions_and_nested_timeouts(self):
        progress = MailFetchProgress(['inbox', 'junkemail'])
        progress.start_unit('inbox')
        progress.start_unit('junkemail')
        progress.finish_unit('inbox', {'success': True, 'emails': [{}, {}]})
        snapshot = progress.snapshot()
        self.assertEqual(snapshot['completed'], 1)
        self.assertEqual(snapshot['active'], ['junkemail'])
        progress.finish_unit('junkemail', {'success': False, 'details': {'error': {'code': 'EMAIL_FETCH_TIMEOUT'}}})
        progress.finish({'success': True, 'partial': True})
        snapshot = progress.snapshot()
        self.assertEqual((snapshot['total'], snapshot['completed'], snapshot['successful'], snapshot['timed_out']), (2, 2, 1, 1))
        self.assertEqual(snapshot['fetched'], 2)
        self.assertEqual(snapshot['state'], 'partial')
        progress.finish_unit('junkemail', {'success': True, 'emails': [{}]})
        self.assertEqual(progress.snapshot(), snapshot)

    def test_partial_account_and_pending_units_are_not_reported_successful(self):
        progress = MailFetchProgress(['a@example.com', 'b@example.com'])
        progress.finish_unit('a@example.com', {'success': True, 'partial': True})
        progress.finish({'success': False, 'error': {'code': 'EMAIL_FETCH_TIMEOUT'}})
        snapshot = progress.snapshot()
        self.assertEqual(snapshot['partial'], 1)
        self.assertEqual(snapshot['failed'], 1)
        self.assertEqual(snapshot['successful'], 0)

    def test_registry_validates_ids_and_expires_requests(self):
        request_id = 'a' * 32
        with patch('outlook_web.mail_fetch_progress._progress_requests', {}):
            self.assertIsNone(begin_mail_fetch_progress('invalid', ['inbox']))
            progress = begin_mail_fetch_progress(request_id, ['inbox'])
            self.assertIsNotNone(progress)
            self.assertIsNone(begin_mail_fetch_progress(request_id, ['other']))
            self.assertEqual(get_mail_fetch_progress(request_id)['total'], 1)
            with patch('outlook_web.mail_fetch_progress.time.monotonic', return_value=progress.updated_at + 1801):
                self.assertIsNone(get_mail_fetch_progress(request_id))

    def test_async_jobs_validate_ids_and_bound_active_work(self):
        with patch('outlook_web.mail_fetch_progress._async_jobs', {
            str(index): {'pending': True, 'updated_at': 0} for index in range(8)
        }):
            with self.assertRaises(ValueError):
                start_async_mail_fetch('invalid', lambda: {})
            with self.assertRaises(RuntimeError):
                start_async_mail_fetch('f' * 32, lambda: {})

    def test_expired_job_returns_explicit_failure(self):
        with patch('outlook_web.mail_fetch_progress._async_jobs', {}):
            result = get_async_mail_fetch_result('a' * 32)
            self.assertFalse(result['pending'])
            self.assertEqual(result['result']['error']['code'], 'MAIL_FETCH_JOB_EXPIRED')


if __name__ == '__main__':
    unittest.main()
