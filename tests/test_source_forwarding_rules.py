import importlib
import os
import tempfile
import unittest


os.environ.setdefault('SECRET_KEY', 'test-secret-key')
os.environ['DATABASE_PATH'] = os.path.join(tempfile.mkdtemp(prefix='source-forwarding-'), 'test.db')
web_outlook_app = importlib.import_module('web_outlook_app')


class SourceForwardingRuleTests(unittest.TestCase):
    def setUp(self):
        self.app = web_outlook_app.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        with self.app.app_context():
            web_outlook_app.init_db()
            db = web_outlook_app.get_db()
            db.execute('DELETE FROM forwarding_policy_channels')
            db.execute('DELETE FROM forwarding_policies')
            db.execute('DELETE FROM notification_channels')
            db.execute("DELETE FROM settings WHERE key LIKE 'source_forwarding%'")
            db.execute("DELETE FROM accounts")
            db.commit()
            web_outlook_app.add_account(
                'rule@example.com', 'secret', account_type='imap', provider='custom',
                imap_host='imap.example.com', imap_password='secret', forward_enabled=True,
            )
            web_outlook_app.set_setting('email_forward_recipient', 'dest@example.com')
            web_outlook_app.set_setting('smtp_host', 'smtp.example.com')
            web_outlook_app.set_setting('smtp_port', '465')
            web_outlook_app.set_setting('forward_channels', 'smtp')
            web_outlook_app.forwarding_store.init_schema(
                db, web_outlook_app.encrypt_data, web_outlook_app.decrypt_data,
            )
            db.commit()

    def test_enabled_account_migrates_to_its_own_rule(self):
        with self.app.app_context():
            account = web_outlook_app.get_account_by_email('rule@example.com')
            route = web_outlook_app.forwarding_store.resolve_policy(
                web_outlook_app.get_db(), account_id=account['id'],
            )
        self.assertEqual(route['mode'], 'custom')
        self.assertEqual(route['path'][0]['scope_type'], 'account')
        self.assertEqual([item['type'] for item in route['channels']], ['smtp'])

    def test_keyword_and_sender_must_both_match(self):
        detail = {
            'subject': '验证码 123456',
            'from': 'Alerts <notify@shop.example>',
            'body': '您的验证码是 123456',
            'body_type': 'text',
        }
        route = {'keywords': '验证码', 'senders': 'notify@shop.example'}
        self.assertTrue(web_outlook_app.forward_email_matches_policy(detail, route))
        self.assertFalse(web_outlook_app.forward_email_matches_policy(
            {**detail, 'from': 'other@shop.example'}, route,
        ))
        self.assertFalse(web_outlook_app.forward_email_matches_policy(
            {**detail, 'subject': '订单发货', 'body': '包裹已发出'}, route,
        ))

    def test_group_rule_is_used_when_account_has_no_rule(self):
        with self.app.app_context():
            db = web_outlook_app.get_db()
            account = web_outlook_app.get_account_by_email('rule@example.com')
            db.execute("DELETE FROM forwarding_policies WHERE scope_type='account'")
            channel_id = db.execute("SELECT id FROM notification_channels WHERE legacy_key='smtp'").fetchone()['id']
            web_outlook_app.forwarding_store.save_policy(db, {
                'scope_type': 'group',
                'scope_id': account['group_id'],
                'mode': 'custom',
                'channel_ids': [channel_id],
                'keywords': '发票',
            })
            db.commit()
            route = web_outlook_app.forwarding_store.resolve_policy(db, account_id=account['id'])
        self.assertEqual(route['mode'], 'custom')
        self.assertEqual(route['keywords'], '发票')
        self.assertEqual(route['path'][0]['mode'], 'inherit')
        self.assertEqual(route['path'][1]['scope_type'], 'group')


if __name__ == '__main__':
    unittest.main()
