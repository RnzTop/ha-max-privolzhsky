"""Durable two-field callback requests. No personal data in logs."""
import json
import logging
import re
import secrets
import time

LOG = logging.getLogger('privolzhsky')
PHONE = '+7 (831) 414-66-99'
TTL = 86400


def profile_name(user):
    user = user or {}
    name = ' '.join(str(user.get(k) or '') for k in ('first_name', 'last_name')).strip()
    return ' '.join((name or user.get('name') or 'Имя не указано в профиле').split())[:120]


def phone_number(value):
    if not re.fullmatch(r'[+()0-9\s-]{10,40}', value):
        return None
    digits = re.sub(r'[^0-9]', '', value)
    if len(digits) == 10:
        digits = '7' + digits
    elif len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    if not 10 <= len(digits) <= 15:
        return None
    return '+' + digits


def view(text, buttons=None):
    result = {'text': text}
    if buttons:
        result['attachments'] = [{'type': 'inline_keyboard', 'payload': {'buttons': [
            [{'type': 'callback', 'text': label, 'payload': payload}]
            for label, payload in buttons]}}]
    return result


class Booking:
    def __init__(self, store, options, default_action):
        self.store, self.options, self.default_action = store, options, default_action
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (
                    chat INTEGER PRIMARY KEY, nonce TEXT, name TEXT, phone TEXT, touched REAL);
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, chat INTEGER, target INTEGER, status TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe TEXT UNIQUE,
                    job TEXT, kind TEXT, request_id TEXT, due REAL, attempts INTEGER DEFAULT 0, created REAL);
            ''')

    def add_job(self, db, key, job, kind='client', request_id=None):
        db.execute('INSERT OR IGNORE INTO outbox(dedupe,job,kind,request_id,due,created) VALUES(?,?,?,?,?,?)',
                   (key, json.dumps(job, ensure_ascii=False), kind, request_id, time.time(), time.time()))

    def plan(self, row):
        """Advance state and save replies in the same transaction as consuming the event."""
        with self.store.connect() as db:
            if not db.execute('SELECT 1 FROM inbox WHERE id=? AND done=0', (row[0],)).fetchone():
                return
            db.execute('DELETE FROM sessions WHERE touched<?', (time.time()-TTL,))
            db.execute("DELETE FROM requests WHERE status!='pending' AND created<?", (time.time()-7*TTL,))
            jobs = self.route(db, json.loads(row[1]))
            for index, (job, kind, request_id) in enumerate(jobs):
                self.add_job(db, row[0]+':'+str(index), job, kind, request_id)
            db.execute('UPDATE inbox SET done=1,payload=NULL WHERE id=?', (row[0],))

    def route(self, db, update):
        kind = update.get('update_type')
        msg = update.get('message') or {}
        recipient = msg.get('recipient') or {}
        sender = (update.get('callback') or {}).get('user') if kind == 'message_callback' else msg.get('sender')
        sender = sender or update.get('user') or {}
        if sender.get('is_bot'):
            return []
        # Setup logs only the numeric group ID, never its messages or membership.
        if kind == 'bot_started':
            db.execute('DELETE FROM sessions WHERE chat=?', (update.get('chat_id'),))
            return self.fallback(update)
        if kind == 'bot_added':
            if self.options.get('setup_mode') and not update.get('is_channel', False):
                LOG.info('Настройка: ID рабочего чата = %s', update.get('chat_id'))
            return []
        text = str((msg.get('body') or {}).get('text') or '').strip()
        chat = recipient.get('chat_id')
        if recipient.get('chat_type') != 'dialog':
            if (self.options.get('setup_mode') and kind == 'message_created'
                    and recipient.get('chat_type') == 'chat' and text == '/chatid' and chat is not None):
                return [(('/messages', {'chat_id': chat}, {'text': 'ID этого рабочего чата: '+str(chat)}), 'client', None)]
            return self.fallback(update)
        if chat is None:
            return []
        callback = update.get('callback') or {}
        payload = str(callback.get('payload') or '') if kind == 'message_callback' else ''
        command = text.lower()
        session = db.execute('SELECT nonce,name,phone FROM sessions WHERE chat=?', (chat,)).fetchone()
        jobs = []

        def reply(body):
            # Acknowledge callback separately: an expired callback cannot lose a form prompt.
            if kind == 'message_callback':
                jobs.append((('/answers', {'callback_id': callback['callback_id']},
                              {'notification': 'Запрос получен'}), 'callback', None))
            jobs.append((('/messages', {'chat_id': chat}, body), 'client', None))
            return jobs

        if payload in ('main', 'cancel') or command in ('/start', '/menu', 'меню', '/cancel', 'отмена'):
            db.execute('DELETE FROM sessions WHERE chat=?', (chat,))
            return self.fallback(dict(update, callback=dict(callback, payload='main'))) if kind == 'message_callback' else self.fallback(dict(update, message=dict(msg, body={'text':'/menu'})))

        if payload == 'booking' or command == 'записаться на ремонт':
            pending = db.execute("SELECT id FROM requests WHERE chat=? AND status='pending'", (chat,)).fetchone()
            if pending:
                return reply(view('Предыдущая заявка ещё ожидает передачи сотрудникам. При срочном вопросе позвоните: '+PHONE,
                                  [('В главное меню', 'main')]))
            if not self.options.get('staff_chat_id'):
                return reply(view('Приём заявок в боте ещё настраивается. Для записи позвоните: '+PHONE,
                                  [('В главное меню', 'main')]))
            nonce = secrets.token_hex(8)
            db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?)',
                       (chat, nonce, profile_name(sender), None, time.time()))
            return reply(view('Оставим заявку на обратный звонок. Имя возьмём из вашего профиля MAX. '
                              'Укажите телефон, по которому сотрудник техцентра сможет связаться с вами. '
                              'Перед отправкой вы проверите данные. Для отмены — /cancel.',
                              [('Отмена', 'cancel')]))

        if payload.startswith('submit:'):
            nonce = payload.split(':', 1)[1]
            if not session or session[0] != nonce or not session[2]:
                return reply(view('Эта кнопка уже использована или устарела. Откройте новую заявку через меню.',
                                  [('Записаться на ремонт', 'booking'), ('В главное меню', 'main')]))
            if not self.options.get('staff_chat_id'):
                return reply(view('Рабочий чат не настроен. Позвоните: '+PHONE, [('Отмена', 'cancel')]))
            target = int(self.options['staff_chat_id'])
            db.execute('INSERT OR IGNORE INTO requests VALUES(?,?,?,?,?)', (nonce, chat, target, 'pending', time.time()))
            # Stable request number lets staff recognize rare duplicates after network timeouts.
            body = view('Заявка на обратный звонок №'+nonce+'\nИмя в MAX: '+session[1]+'\nТелефон: '+session[2]+
                        '\nСвяжитесь с клиентом, уточните услугу и согласуйте время. Запись ещё не подтверждена.')
            self.add_job(db, 'staff:'+nonce, ('/messages', {'chat_id': target}, body), 'staff', nonce)
            db.execute('DELETE FROM sessions WHERE chat=?', (chat,))
            return reply(view('Заявка сохранена и ожидает передачи сотрудникам. Сообщу здесь, когда MAX примет её в рабочий чат. '
                              'Время посещения согласует сотрудник при звонке.', [('В главное меню', 'main')]))

        if payload.startswith('edit:'):
            if session and payload == 'edit:'+session[0]:
                db.execute('UPDATE sessions SET phone=NULL,touched=? WHERE chat=?', (time.time(), chat))
                return reply(view('Введите правильный телефон:', [('Отмена', 'cancel')]))
            return reply(view('Форма устарела. Начните новую заявку.', [('Записаться на ремонт', 'booking')]))

        if session and kind == 'message_created':
            number = phone_number(text)
            if not number:
                return reply(view('Не удалось распознать телефон. Введите номер цифрами, например +7 900 000-00-00. '
                                  'Можно использовать пробелы, скобки и дефисы.', [('Отмена', 'cancel')]))
            db.execute('UPDATE sessions SET phone=?,touched=? WHERE chat=?', (number, time.time(), chat))
            return reply(view('Проверьте заявку на обратный звонок:\nИмя в MAX: '+session[1]+'\nТелефон: '+number+
                              '\n\nНажимая «Передать сотрудникам», вы подтверждаете передачу этих данных сотрудникам '
                              'техцентра «Приволжский» в закрытый рабочий чат MAX для обратного звонка и согласования записи. '
                              'Это ещё не подтверждённая запись.',
                              [('Передать сотрудникам', 'submit:'+session[0]),
                               ('Изменить телефон', 'edit:'+session[0]), ('Отмена', 'cancel')]))
        if kind == 'message_callback' and payload == 'contacts':
            db.execute('DELETE FROM sessions WHERE chat=?', (chat,))
        return self.fallback(update)

    def fallback(self, update):
        job = self.default_action(update)
        return [] if job is None else [(job, 'client', None)]

    def next_job(self):
        with self.store.connect() as db:
            # Preserve per-recipient order; failure in staff chat does not block other chats.
            db.execute('DELETE FROM sessions WHERE touched<?', (time.time()-TTL,))
            rows = db.execute('SELECT id,job,kind,request_id,attempts,due,created FROM outbox ORDER BY id').fetchall()
        blocked = set()
        for row in rows:
            job = json.loads(row[1])
            target = str(job[1].get('chat_id', 'callback:'+str(row[0])))
            if target in blocked:
                continue
            blocked.add(target)
            if row[5] <= time.time():
                return row
        return None

    def expired(self, row):
        return len(row) > 6 and time.time()-row[6] >= TTL

    def finish(self, row):
        with self.store.connect() as db:
            if row[2] == 'staff':
                req = db.execute('SELECT chat FROM requests WHERE id=?', (row[3],)).fetchone()
                if req:
                    db.execute("UPDATE requests SET status='delivered' WHERE id=?", (row[3],))
                    self.add_job(db, 'receipt:'+row[3], ('/messages', {'chat_id': req[0]}, view(
                        'Заявка передана в рабочий чат техцентра. Сотрудник свяжется с вами по указанному телефону, '
                        'уточнит услугу и согласует время. Запись пока не подтверждена.', [('В главное меню', 'main')])))
                    LOG.info('Заявка передана сотрудникам')
            db.execute('DELETE FROM outbox WHERE id=?', (row[0],))

    def fail(self, row, status):
        with self.store.connect() as db:
            permanent = status in (400, 403, 404)
            req = db.execute('SELECT chat,created FROM requests WHERE id=?', (row[3],)).fetchone() if row[2]=='staff' else None
            expired = req and time.time()-req[1] >= TTL
            if permanent or expired or self.expired(row):
                LOG.warning('Отправка %s завершена без доставки, HTTP %s', row[2], status)
                if req:
                    db.execute("UPDATE requests SET status='failed' WHERE id=?", (row[3],))
                    self.add_job(db, 'failure:'+row[3], ('/messages', {'chat_id':req[0]}, view(
                        'Не удалось передать заявку сотрудникам. Пожалуйста, позвоните для записи: '+PHONE,
                        [('В главное меню','main')])))
                    LOG.error('Заявка не передана сотрудникам, HTTP %s. Проверьте ID и права рабочего чата.', status)
                db.execute('DELETE FROM outbox WHERE id=?', (row[0],))
            else:
                delay = min(2**min(row[4]+1, 6), 60)
                db.execute('UPDATE outbox SET attempts=attempts+1,due=? WHERE id=?', (time.time()+delay, row[0]))
                if req and row[4] == 2:
                    self.add_job(db, 'delay:'+row[3], ('/messages', {'chat_id':req[0]}, view(
                        'Передача заявки задерживается из-за ошибки связи с MAX. Повторяем попытки. '
                        'Если вопрос срочный, позвоните: '+PHONE)))
                LOG.warning('Отправка %s не выполнена, HTTP %s; повтор через %s с', row[2], status, delay)
