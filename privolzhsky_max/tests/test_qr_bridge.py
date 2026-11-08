import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from unittest.mock import Mock
from pathlib import Path

from booking import Booking
from bot import Store, action, validate
from qr_bridge import enqueue, handler


class BridgeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / 'bot.sqlite3')
        self.options = dict(mode='polling', staff_chat_id='-42',
                            qr_bridge_secret='s' * 32, access_token='fake')
        validate(self.options)
        self.workflow = Booking(self.store, self.options, action)
        self.event = dict(kind='message', account_id='work', dialog_id='d1',
                          message_id='m1', name='Клиент', text='Добрый день')

    def test_concurrent_retry_and_restart(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(lambda _: enqueue(self.workflow, self.event), range(8))),
                             ['queued'] * 8)
        job = self.workflow.next_job()
        self.workflow.finish(job)
        restarted = Booking(self.store, self.options, action)
        self.assertEqual(enqueue(restarted, self.event), 'delivered')
        self.assertIsNone(restarted.next_job())
        for change in (dict(dialog_id='d2'), dict(message_id='m2'), dict(account_id='other')):
            self.assertEqual(enqueue(restarted, dict(self.event, **change)), 'queued')
            self.workflow.finish(self.workflow.next_job())

    def test_group_claim(self):
        enqueue(self.workflow, self.event)
        outgoing = self.workflow.next_job()
        _, target, card = json.loads(outgoing[1])
        self.assertEqual(target, {'chat_id': -42})
        self.workflow.finish(outgoing)
        payload = card['attachments'][0]['payload']['buttons'][0][0]['payload']
        for i, chat, actor, name in ((1, -43, 1, 'Чужой'), (2, -42, 2, 'Анна'), (3, -42, 3, 'Олег')):
            self.store.enqueue([dict(update_type='message_callback',
                                     callback=dict(payload=payload, callback_id=str(i),
                                                   user=dict(user_id=actor, first_name=name)),
                                     message=dict(recipient=dict(chat_type='chat', chat_id=chat),
                                                  body=dict(text=card['text'])))])
            self.workflow.plan(self.store.next())
            job = self.workflow.next_job()
            if i == 1:
                self.assertIsNone(job)
                continue
            path, params, answer = json.loads(job[1])
            self.assertEqual((path, params), ('/answers', {'callback_id': str(i)}))
            self.assertTrue(answer['message']['text'].endswith('В работе: Анна'))
            self.assertEqual(answer['message']['attachments'], [])
            self.workflow.finish(job)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT assignee_id FROM qr_events').fetchone(), (2,))

    def test_auth_http_and_failed_status(self):
        handler_class = handler(self.workflow, self.options['qr_bridge_secret'])
        event = dict(kind='auth_lost', account_id='work', transition_id='1')

        def send(secret, payload):
            body = json.dumps(payload).encode()
            receiver = handler_class.__new__(handler_class)
            receiver.path = '/qr/v1/events'
            receiver.headers = {'Authorization': 'Bearer ' + secret, 'Content-Length': str(len(body))}
            receiver.connection = Mock()
            receiver.rfile, receiver.wfile = BytesIO(body), BytesIO()
            receiver.send_error = receiver.send_response = Mock()
            receiver.send_header = receiver.end_headers = Mock()
            receiver.do_POST()
            return receiver.send_response.call_args.args[0], receiver.wfile.getvalue()

        secret = self.options['qr_bridge_secret']
        self.assertEqual(send('wrong', event)[0], 403)
        self.assertEqual(send(secret, dict(event, transition_id=''))[0], 400)
        self.assertEqual(send(secret, event)[0], 202)
        self.workflow.fail(self.workflow.next_job(), 403)
        self.assertEqual(json.loads(send(secret, event)[1]), {'status': 'failed'})
        self.assertEqual(send(secret, dict(event, kind='auth_restored'))[0], 202)

    def test_regular_booking_without_bridge(self):
        workflow = Booking(self.store, dict(mode='polling', staff_chat_id='-42'), action)
        self.store.enqueue([dict(update_type='bot_started', chat_id=10)])
        workflow.plan(self.store.next())
        self.assertEqual(json.loads(workflow.next_job()[1])[:2], ['/messages', {'chat_id': 10}])
