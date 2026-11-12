import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest

import bridge


class FakeBionic(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        body = json.dumps({'choices':[{'message':{'content':data['messages'][0]['content']}}]}).encode()
        self.send_response(200)
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = ThreadingHTTPServer(('127.0.0.1',0),FakeBionic)
        bridge.BACKEND = 'http://127.0.0.1:'+str(cls.backend.server_port)
        cls.server = bridge.Server(('127.0.0.1',0),'s'*40)
        for server in (cls.backend,cls.server):
            threading.Thread(target=server.serve_forever,daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.server,cls.backend):
            server.shutdown(); server.server_close()

    def request(self, method, path, data=None, auth='Bearer '+('s'*40)):
        conn = http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        conn.request(method,path,body=json.dumps(data) if data is not None else None,headers={'Authorization':auth})
        response=conn.getresponse()
        result=response.status,json.loads(response.read())
        conn.close()
        return result

    def test_requires_key_and_restricts_route_and_model(self):
        valid={'model':bridge.MODEL,'messages':[{'role':'user','content':'hello'}],'max_tokens':50}
        self.assertEqual(self.request('POST','/v1/chat/completions',valid,auth='')[0],401)
        self.assertEqual(self.request('POST','/v1/other',valid)[0],404)
        self.assertEqual(self.request('POST','/v1/chat/completions',{**valid,'model':'other'})[0],400)
        self.assertEqual(self.request('POST','/v1/chat/completions',{**valid,'stream':True})[0],400)
        self.assertEqual(self.request('POST','/v1/chat/completions',{**valid,'tools':[]})[0],400)
        status, body=self.request('POST','/v1/chat/completions',valid)
        self.assertEqual(status,200)
        self.assertEqual(body['choices'][0]['message']['content'],'hello')

    def test_busy_capacity(self):
        self.server.slot.acquire()
        try:
            request={'model':bridge.MODEL,'messages':[{'role':'user','content':'hello'}]}
            self.assertEqual(self.request('POST','/v1/chat/completions',request)[0],503)
        finally: self.server.slot.release()


if __name__=='__main__': unittest.main()
