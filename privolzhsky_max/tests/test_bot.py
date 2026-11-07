import sys,unittest,tempfile,json,threading,http.client
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bot
class Tests(unittest.TestCase):
 def test_pages(self):
  for key in bot.TEXT:
   p=bot.page(key); self.assertTrue(p['text']); self.assertTrue(p['attachments'][0]['payload']['buttons'])
  self.assertEqual(len(bot.page('main')['attachments'][0]['payload']['buttons']),2)
 def test_routing(self):
  self.assertEqual(bot.action({'update_type':'bot_started','chat_id':42})[1],{'chat_id':42})
  msg={'recipient':{'chat_type':'dialog','chat_id':42},'sender':{'is_bot':False},'body':{'text':'xxx'}}
  self.assertEqual(bot.action({'update_type':'message_created','message':msg})[2]['text'],bot.TEXT['unknown'])
  msg['body']['text']='/start'; self.assertEqual(bot.action({'update_type':'message_created','message':msg})[2],bot.page('main'))
  msg['recipient']['chat_type']='chat'; self.assertIsNone(bot.action({'update_type':'message_created','message':msg}))
  msg['recipient']['chat_type']='dialog'
  for key in bot.TEXT:
   job=bot.action({'update_type':'message_callback','message':msg,'callback':{'callback_id':'x','payload':key}})
   self.assertEqual(job[0],'/answers'); self.assertEqual(job[2]['message'],bot.page(key))
 def test_store(self):
  with tempfile.TemporaryDirectory() as d:
   s=bot.Store(d+'/db'); u={'update_type':'bot_started','chat_id':1}
   s.enqueue([u,u],123); self.assertEqual(s.marker(),123); row=s.next(); s.done(row[0]); self.assertIsNone(s.next())
   s.enqueue([u]); self.assertIsNone(s.next()); self.assertEqual(bot.Store(d+'/db').marker(),123)
 def test_validation(self):
  with self.assertRaises(ValueError): bot.validate({'access_token':''})
  o={'access_token':'fake','mode':'webhook','webhook_url':'https://example.org/max/webhook','webhook_secret':'a'*32}; bot.validate(o)
  for url in ('http://example.org/max/webhook','https://example.org:8123/max/webhook','https://example.org/foo'):
   with self.assertRaises(ValueError): bot.validate(dict(o,webhook_url=url))
 def test_webhook(self):
  with tempfile.TemporaryDirectory() as d:
   s=bot.Store(d+'/db'); server=bot.ThreadingHTTPServer(('127.0.0.1',0),bot.handler(s,'secret'))
   threading.Thread(target=server.serve_forever,daemon=True).start()
   try:
    for secret,status in [('bad',403),('secret',200)]:
     c=http.client.HTTPConnection('127.0.0.1',server.server_port); c.request('POST','/max/webhook',json.dumps({'update_type':'bot_started','chat_id':1}),{'X-Max-Bot-Api-Secret':secret}); r=c.getresponse(); self.assertEqual(r.status,status); r.read(); c.close()
    self.assertIsNotNone(s.next())
   finally: server.shutdown(); server.server_close()
if __name__=='__main__': unittest.main()
