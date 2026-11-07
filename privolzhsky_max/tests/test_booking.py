import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot
from booking import Booking, phone_number, profile_name


def event(text=None, payload=None, user=None, chat=123, group=False, stamp=0):
    msg={'recipient':{'chat_type':'chat' if group else 'dialog','chat_id':chat},
         'sender':user or {'user_id':1,'first_name':'Иван','last_name':'Иванов'},'body':{'text':text or ''}}
    result={'update_type':'message_created','timestamp':stamp,'message':msg}
    if payload is not None:
        result.update(update_type='message_callback',callback={'user':msg['sender'],'callback_id':str(stamp),'payload':payload})
    return result


class BookingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=bot.Store(self.tmp.name+'/db')
        self.flow=Booking(self.store,{'staff_chat_id':'-999'},bot.action)
        self.stamp=0

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, **kwargs):
        self.stamp+=1
        self.store.enqueue([event(stamp=self.stamp,**kwargs)])
        row=self.store.next(); self.flow.plan(row)
        return row

    def rows(self):
        with self.store.connect() as db:
            return db.execute('SELECT id,job,kind,request_id,attempts,due FROM outbox ORDER BY id').fetchall()

    def clear_clients(self):
        for row in self.rows():
            if row[2]!='staff': self.flow.finish(row)

    def session(self):
        with self.store.connect() as db:
            return db.execute('SELECT nonce,name,phone FROM sessions WHERE chat=123').fetchone()

    def test_only_phone_and_auto_name(self):
        self.plan(payload='booking')
        self.assertEqual(self.session()[1],'Иван Иванов')
        self.plan(text='8 (900) 123-45-67')
        nonce,name,phone=self.session(); self.assertEqual(phone,'+79001234567')
        self.assertFalse(any(r[2]=='staff' for r in self.rows()))
        confirmation=json.loads(self.rows()[-1][1])[2]
        self.assertIn('закрытый рабочий чат',confirmation['text'])
        self.assertEqual(confirmation['attachments'][0]['payload']['buttons'][0][0]['payload'],'submit:'+nonce)
        self.plan(payload='submit:'+nonce)
        staff=[r for r in self.rows() if r[2]=='staff']
        self.assertEqual(len(staff),1)
        body=json.loads(staff[0][1]); self.assertEqual(body[1],{'chat_id':-999})
        self.assertIn('Иван Иванов',body[2]['text']); self.assertIn(phone,body[2]['text'])
        self.assertIsNone(self.session())
        self.plan(payload='submit:'+nonce)
        self.assertEqual(len([r for r in self.rows() if r[2]=='staff']),1)

    def test_invalid_phone_cancel_and_stale_button(self):
        self.plan(payload='booking'); old=self.session()[0]
        self.plan(text='не телефон'); self.assertIsNone(self.session()[2])
        self.plan(text='/cancel'); self.assertIsNone(self.session())
        self.plan(payload='booking'); self.plan(text='+79001234567')
        self.plan(payload='submit:'+old)
        self.assertFalse(any(r[2]=='staff' for r in self.rows()))
        self.assertIsNotNone(self.session())
        self.plan(payload='edit:'+self.session()[0]); self.assertIsNone(self.session()[2])

    def test_restart_and_duplicate_event(self):
        row=self.plan(payload='booking'); before=self.session(); count=len(self.rows())
        self.flow.plan(row); self.assertEqual(len(self.rows()),count)
        self.flow=Booking(bot.Store(self.tmp.name+'/db'),{'staff_chat_id':'-999'},bot.action)
        self.assertEqual(self.session(),before)
        self.plan(text='+79001234567'); nonce=self.session()[0]
        row=self.plan(payload='submit:'+nonce); self.flow.plan(row)
        self.assertEqual(len([r for r in self.rows() if r[2]=='staff']),1)

    def test_missing_configuration_no_collection_and_no_group_forms(self):
        self.flow.options={}
        self.plan(payload='booking'); self.assertIsNone(self.session())
        self.flow.options={'staff_chat_id':'-999'}
        self.plan(payload='booking',group=True); self.assertIsNone(self.session())
        self.plan(text='/chatid',group=True); self.assertFalse(any('ID этого' in r[1] for r in self.rows()))
        self.flow.options['setup_mode']=True; self.plan(text='/chatid',group=True)
        self.assertTrue(any('ID этого' in r[1] for r in self.rows()))

    def test_no_success_until_staff_send_and_failure_notice(self):
        self.plan(payload='booking'); self.plan(text='+79001234567'); nonce=self.session()[0]
        self.plan(payload='submit:'+nonce); self.clear_clients()
        row=self.flow.next_job(); self.assertEqual(row[2],'staff')
        self.flow.fail(row,503)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM requests').fetchone()[0],'pending')
        self.assertFalse(any('receipt:' in str(r) for r in self.rows()))
        self.flow.fail(row,403)
        self.assertFalse(any(r[2]=='staff' for r in self.rows()))
        self.assertTrue(any('Не удалось передать' in r[1] for r in self.rows()))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM requests').fetchone()[0],'failed')

    def test_success_removes_phone_and_queues_receipt(self):
        self.plan(payload='booking'); self.plan(text='+79001234567'); nonce=self.session()[0]
        self.plan(payload='submit:'+nonce); self.clear_clients()
        row=self.flow.next_job(); self.flow.finish(row)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM requests').fetchone()[0],'delivered')
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0],0)
        self.assertTrue(any('Заявка передана в рабочий чат' in r[1] for r in self.rows()))
        self.assertFalse(any('+79001234567' in r[1] for r in self.rows()))

    def test_failure_does_not_block_other_clients(self):
        self.plan(payload='booking'); self.plan(text='+79001234567'); self.plan(payload='submit:'+self.session()[0]); self.clear_clients()
        row=self.flow.next_job(); self.flow.fail(row,503)
        self.plan(payload='contacts',chat=456)
        self.assertEqual(json.loads(self.flow.next_job()[1])[1],{'callback_id':str(self.stamp)})

    def test_names_phone_and_main_menu(self):
        self.assertEqual(profile_name({'name':'Пётр'}),'Пётр')
        self.assertEqual(profile_name({}),'Имя не указано в профиле')
        self.assertIsNone(phone_number('123'))
        self.assertIsNone(phone_number('abc79001234567'))
        self.assertEqual(set(bot.LABELS),{'booking','contacts'})
        self.assertEqual(bot.page('faq'),bot.page('unknown'))
        self.assertNotIn('Услуги',bot.page('main')['text'])


    def test_expired_request_not_sent(self):
        self.plan(payload='booking'); self.plan(text='+79001234567'); self.plan(payload='submit:'+self.session()[0]); self.clear_clients()
        with self.store.connect() as db:
            db.execute('UPDATE outbox SET created=?',(time.time()-90000,))
            db.execute('UPDATE requests SET created=?',(time.time()-90000,))
        row=self.flow.next_job(); self.assertTrue(self.flow.expired(row)); self.flow.fail(row,0)
        self.assertFalse(any(r[2]=='staff' for r in self.rows()))
        self.assertTrue(any('Не удалось передать' in r[1] for r in self.rows()))

    def test_native_booking_through_worker(self):
        from unittest.mock import patch
        self.plan(payload='booking'); self.clear_clients()
        self.plan(text='+79001234567'); self.clear_clients()
        self.plan(payload='submit:'+self.session()[0])
        calls=[]
        class FakeStop:
            checks=0
            def is_set(self):
                self.checks+=1
                return self.checks>15
            def wait(self, timeout): return False
        class FakeAPI:
            def call(self,method,path,params,body):
                calls.append((path,params,body))
                return {'success':True}
        with patch.object(bot,'STOP',FakeStop()):
            bot.worker(self.store,FakeAPI(),{'staff_chat_id':'-999'})
        staff=[c for c in calls if c[1].get('chat_id')==-999]
        self.assertEqual(len(staff),1)
        success=[c for c in calls if 'Заявка передана в рабочий чат' in c[2].get('text','')]
        self.assertEqual(len(success),1)
        self.assertLess(calls.index(staff[0]),calls.index(success[0]))

if __name__=='__main__': unittest.main()
