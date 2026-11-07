"""MAX HA app: stdlib only. No credentials or customer text in logs."""
import hashlib
import hmac
import json
import logging
import re
import signal
import sqlite3
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import request, parse, error
from booking import Booking

LOG = logging.getLogger('privolzhsky')
STOP = threading.Event()
TYPES = ['bot_started', 'message_created', 'message_callback', 'bot_added']
PHONE = '+7 (831) 414-66-99'
LABELS = {'booking': 'Записаться на ремонт', 'contacts': 'Контакты'}
TEXT = {
    'main': 'Здравствуйте! Это помощник техцентра «Приволжский» в Нижнем Новгороде. Здесь можно оставить заявку на обратный звонок для записи на ремонт и найти наши контакты. Выберите нужный раздел.',
    'booking': 'Оставьте заявку на обратный звонок через кнопку «Записаться на ремонт».',
    'contacts': f'Техцентр «Приволжский»\nНижний Новгород, Базовый проезд, 3\nТелефон: {PHONE}\nПн–чт: 09:00–18:00\nПт: 09:00–17:00\nСб и вс: выходные.',
    'unknown': f'Выберите раздел в меню ниже. По другим вопросам позвоните в техцентр: {PHONE}.'}

def button(text, payload):
    return {'type':'callback','text':text,'payload':payload}

def page(key):
    key = key if key in TEXT else 'unknown'
    rows=[]
    if key in ('main','unknown'):
        rows = [[button(label,k)] for k,label in LABELS.items()]
    elif key == 'booking':
        rows = [[button('Записаться на ремонт','booking')]]
    elif key == 'contacts':
        rows = [[{'type':'link','text':'Показать на карте','url':'https://yandex.ru/maps/org/1054778071/'}]]
    if key not in ('main','unknown'):
        rows.append([button('В главное меню','main')])
    return {'text':TEXT[key],'attachments':[{'type':'inline_keyboard','payload':{'buttons':rows}}]}

def action(update):
    kind=update.get('update_type')
    msg=update.get('message') or {}
    recipient=msg.get('recipient') or {}
    if kind == 'bot_started':
        return ('/messages', {'chat_id':update['chat_id']}, page('main'))
    # Only personal dialogs; group chats/channels and other bots are ignored.
    if recipient.get('chat_type') != 'dialog':
        return None
    if kind == 'message_callback':
        cb=update.get('callback') or {}
        return ('/answers',{'callback_id':cb['callback_id']},{'message':page(cb.get('payload'))})
    if kind == 'message_created':
        if (msg.get('sender') or {}).get('is_bot'):
            return None
        text=((msg.get('body') or {}).get('text') or '').strip().lower()
        keys={v.lower():k for k,v in LABELS.items()}
        key='main' if text in ('/start','/menu','меню','старт') else keys.get(text,'unknown')
        return ('/messages',{'chat_id':recipient['chat_id']},page(key))
    return None

class APIError(Exception):
    def __init__(self,status=0): self.status=status

class API:
    def __init__(self,token):
        self.token=token
        self.context=ssl.create_default_context()
        for cert in (Path(__file__).parent/'certs').glob('*.pem'):
            self.context.load_verify_locations(str(cert))
    def call(self,method,path,params=None,body=None):
        url='https://platform-api2.max.ru'+path
        if params: url+='?'+parse.urlencode(params)
        data=None if body is None else json.dumps(body).encode()
        req=request.Request(url,data=data,method=method,headers={'Authorization':self.token,'Content-Type':'application/json'})
        try:
            with request.urlopen(req,context=self.context,timeout=45) as res:
                result=json.load(res)
            if result.get('success') is False: raise APIError(400)
            return result
        except error.HTTPError as exc:
            raise APIError(exc.code) from None
        except (error.URLError,TimeoutError,OSError,ValueError):
            raise APIError() from None

class Store:
    def __init__(self,path):
        self.path=path
        with self.connect() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS inbox (id TEXT PRIMARY KEY, payload TEXT, done INTEGER DEFAULT 0, created REAL); CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);')
    def connect(self): return sqlite3.connect(self.path,timeout=10)
    def enqueue(self,updates,marker=None):
        with self.connect() as db:
            for update in updates:
                raw=json.dumps(update,sort_keys=True,ensure_ascii=False)
                key=hashlib.sha256(raw.encode()).hexdigest()
                db.execute('INSERT OR IGNORE INTO inbox(id,payload,created) VALUES(?,?,?)',(key,raw,time.time()))
            if marker is not None:
                db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',('marker',str(marker)))
            db.execute('DELETE FROM inbox WHERE done=1 AND created<?',(time.time()-7*86400,))
    def marker(self):
        with self.connect() as db:
            row=db.execute("SELECT value FROM state WHERE key='marker'").fetchone()
            return None if row is None else int(row[0])
    def next(self):
        with self.connect() as db:
            return db.execute('SELECT id,payload FROM inbox WHERE done=0 ORDER BY rowid LIMIT 1').fetchone()
    def done(self,key):
        with self.connect() as db:
            db.execute('UPDATE inbox SET done=1,payload=NULL WHERE id=?',(key,))

def worker(store,api,options):
    workflow = Booking(store, options, action)
    while not STOP.is_set():
        row = store.next()
        if row:
            try:
                workflow.plan(row)
            except (KeyError, TypeError, ValueError):
                LOG.warning('Некорректное событие пропущено'); store.done(row[0])
        outgoing = workflow.next_job()
        if outgoing:
            if workflow.expired(outgoing):
                workflow.fail(outgoing, 0)
                continue
            job = json.loads(outgoing[1])
            try:
                api.call('POST', job[0], job[1], job[2])
                workflow.finish(outgoing)
                LOG.info('Ответ обработан')
            except APIError as exc:
                workflow.fail(outgoing, exc.status)
            STOP.wait(.6)
        elif not row:
            STOP.wait(.3)


def handler(store,secret):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            self.send_response(200 if self.path == '/health' else 404); self.end_headers()
        def do_POST(self):
            if self.path != '/max/webhook':
                self.send_error(404); return
            if not hmac.compare_digest(self.headers.get('X-Max-Bot-Api-Secret',''),secret):
                self.send_error(403); return
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0 < size <= 262144: raise ValueError()
                self.connection.settimeout(5)
                update=json.loads(self.rfile.read(size))
                if not isinstance(update,dict) or 'update_type' not in update: raise ValueError()
                store.enqueue([update])
            except (ValueError,TimeoutError,OSError):
                self.send_error(400); return
            except sqlite3.Error:
                self.send_error(503); return
            self.send_response(200); self.end_headers(); self.wfile.write(b'OK')
    return Handler

def validate(options):
    if not options.get('access_token','').strip(): raise ValueError('Введите access_token в настройках дополнения')
    if options.get('mode') not in ('polling','webhook'): raise ValueError('Неверный mode')
    target=str(options.get('staff_chat_id','')).strip()
    if target and (not re.fullmatch(r'-?[0-9]+',target) or not -(2**63) <= int(target) < 2**63 or int(target)==0):
        raise ValueError('staff_chat_id должен быть числовым ID рабочего чата')
    options['staff_chat_id']=target
    if options['mode']=='webhook':
        u=parse.urlsplit(options.get('webhook_url',''))
        if u.scheme!='https' or not u.hostname or u.port not in (None,443) or u.path!='/max/webhook' or u.query or u.fragment or u.username:
            raise ValueError('webhook_url должен быть https://ваш-домен/max/webhook на порту 443')
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,256}',options.get('webhook_secret','')):
            raise ValueError('webhook_secret: 16–256 латинских букв, цифр, _ или -')

def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    options=json.loads(Path('/data/options.json').read_text())
    try: validate(options)
    except ValueError as exc: LOG.error('%s',exc); return 1
    api=API(options['access_token'].strip()); store=Store('/data/bot.sqlite3')
    signal.signal(signal.SIGTERM,lambda *_: STOP.set())
    signal.signal(signal.SIGINT,lambda *_: STOP.set())
    server=None
    if options['mode']=='webhook':
        server=ThreadingHTTPServer(('0.0.0.0',8080),handler(store,options['webhook_secret']))
        server.daemon_threads=True
        threading.Thread(target=server.serve_forever,daemon=True).start()
    # Retry startup read-only checks on network failures, never delete other subscriptions.
    while not STOP.is_set():
        try:
            step = 'GET /me'
            api.call('GET','/me')
            if options.get('staff_chat_id'):
                step = 'GET рабочего чата'
                staff = api.call('GET', '/chats/'+options['staff_chat_id'])
                if staff.get('type') != 'chat' or staff.get('status') != 'active':
                    LOG.error('staff_chat_id должен указывать на активную рабочую группу с добавленным ботом.'); return 1
                LOG.info('Рабочая группа доступна боту')
            step = 'GET /subscriptions'
            subscriptions=api.call('GET','/subscriptions').get('subscriptions',[])
            if options['mode']=='polling' and subscriptions:
                LOG.error('Есть Webhook-подписка. Выберите webhook или удалите старую подписку осознанно в MAX.'); return 1
            if options['mode']=='webhook':
                if any(s['url']!=options['webhook_url'] for s in subscriptions):
                    LOG.error('Обнаружена другая Webhook-подписка; запуск остановлен во избежание двойных ответов.'); return 1
                step = 'POST /subscriptions'
                api.call('POST','/subscriptions',body={'url':options['webhook_url'],'update_types':TYPES,'secret':options['webhook_secret']})
            break
        except APIError as exc:
            LOG.warning('Проверка MAX: %s не выполнен, HTTP %s. Повтор через 30 с',step,exc.status); STOP.wait(30)
    if STOP.is_set(): return 0
    threading.Thread(target=worker,args=(store,api,options),daemon=True).start()
    LOG.info('Бот запущен: %s; заявки %s',options['mode'], 'включены' if options.get('staff_chat_id') else 'ожидают настройки рабочего чата')
    if options['mode']=='polling':
        LOG.warning('Long Polling предназначен только для проверки. Для клиентов настройте Webhook.')
        while not STOP.is_set():
            try:
                params={'timeout':30,'limit':100,'types':','.join(TYPES)}
                if store.marker() is not None: params['marker']=store.marker()
                result=api.call('GET','/updates',params)
                store.enqueue(result.get('updates',[]),result.get('marker'))
                STOP.wait(.35)
            except APIError as exc:
                LOG.warning('Получение событий не выполнено, HTTP %s; повтор через 10 с',exc.status); STOP.wait(10)
    else:
        while not STOP.wait(1): pass
    if server: server.shutdown()
    return 0

if __name__=='__main__':
    raise SystemExit(main())
