"""Private, opt-in ingress for normalized events from the QR reader."""
import hashlib
import hmac
import json
import re
import secrets
from http.server import BaseHTTPRequestHandler
from sqlite3 import Error

from booking import profile_name, view

PREFIX = 'qr:claim:'
ID = re.compile(r'[A-Za-z0-9._:-]{1,160}')


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS qr_events (
        source_key TEXT PRIMARY KEY, card TEXT UNIQUE, target INTEGER NOT NULL,
        assignee TEXT, assignee_id INTEGER, status TEXT NOT NULL DEFAULT 'queued')''')


def enqueue(workflow, event):
    if not isinstance(event, dict):
        raise ValueError()
    kind = event.get('kind')
    keys = ('account_id', 'dialog_id', 'message_id') if kind == 'message' else ('account_id', 'transition_id')
    identifiers = [event.get(key) for key in keys]
    if any(not isinstance(value, str) or not ID.fullmatch(value) for value in identifiers):
        raise ValueError()
    card = None
    if kind == 'message':
        name, message = event.get('name'), event.get('text')
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError()
        if not isinstance(message, str) or not message.strip() or len(message) > 3000:
            raise ValueError()
        card = secrets.token_hex(16)
    elif kind not in ('auth_lost', 'auth_restored'):
        raise ValueError()

    key = hashlib.sha256(json.dumps([kind, *identifiers]).encode()).hexdigest()
    target = int(workflow.options['staff_chat_id'])
    with workflow.store.connect() as db:
        inserted = db.execute('INSERT OR IGNORE INTO qr_events(source_key,card,target) VALUES(?,?,?)',
                              (key, card, target)).rowcount
        if inserted:
            if card:
                body = view(f'Обращение MAX №{card[:12]}\n{name}\n\n{message}',
                            [('Взять в работу', PREFIX + card)])
            else:
                body = view('Рабочий аккаунт MAX потерял авторизацию. Повторите вход по QR-коду.'
                            if kind == 'auth_lost' else 'Авторизация рабочего аккаунта MAX восстановлена.')
            workflow.add_job(db, 'qr:' + key, ('/messages', {'chat_id': target}, body), 'qr', key)
        return db.execute('SELECT status FROM qr_events WHERE source_key=?', (key,)).fetchone()[0]


def claim(db, options, update, sender):
    callback = update['callback']
    msg = update.get('message') or {}
    recipient = msg.get('recipient') or {}
    staff = options.get('staff_chat_id')
    if (not options.get('qr_bridge_secret') or options.get('mode') != 'polling'
            or not staff or recipient.get('chat_type') != 'chat'
            or recipient.get('chat_id') != int(staff)
            or type(sender.get('user_id')) is not int or sender['user_id'] <= 0):
        return []
    card = callback['payload'][len(PREFIX):]
    callback_id = callback.get('callback_id')
    text = (msg.get('body') or {}).get('text')
    if (not re.fullmatch(r'[0-9a-f]{32}', card) or not isinstance(callback_id, str)
            or not callback_id or not isinstance(text, str)
            or not text.startswith(f'Обращение MAX №{card[:12]}\n')):
        return []
    name = profile_name(sender)
    first = db.execute('UPDATE qr_events SET assignee=?, assignee_id=? WHERE card=? AND target=? AND assignee IS NULL',
                       (name, sender['user_id'], card, int(staff))).rowcount
    row = db.execute('SELECT assignee FROM qr_events WHERE card=? AND target=?',
                     (card, int(staff))).fetchone()
    if not row:
        return []
    body = {'notification': 'Вы взяли обращение в работу' if first else 'Ответственный: ' + row[0]}
    # A later click on a duplicate or on a card whose first answer failed
    # can still render the assignment persisted by the first click.
    body['message'] = {'text': text + '\n\nВ работе: ' + row[0], 'attachments': []}
    return [(('/answers', {'callback_id': callback_id}, body), 'callback', None)]


def handler(workflow, secret):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.path != '/qr/v1/events':
                self.send_error(404)
                return
            provided = self.headers.get('Authorization', '').encode('utf-8')
            if not hmac.compare_digest(provided, ('Bearer ' + secret).encode()):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 32768:
                    raise ValueError()
                self.connection.settimeout(5)
                status = enqueue(workflow, json.loads(self.rfile.read(size)))
            except (ValueError, OSError):
                self.send_error(400)
                return
            except Error:
                self.send_error(503)
                return
            body = json.dumps({'status': status}).encode()
            self.send_response(202 if status == 'queued' else 200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler
