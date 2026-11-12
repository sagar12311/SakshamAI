"""Private Tailscale-bound, authenticated bridge to Bionic's loopback API.

No prompts, responses, credentials, or request paths are written to logs.
The target, routes and model are fixed: this is not a general proxy.
"""
import hmac
import ipaddress
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

MAX_BODY = 131072
MAX_REPLY = 2097152
MODEL = 'qwen/qwen3.5-9b'
BACKEND = 'http://127.0.0.1:1235'


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 4

    def __init__(self, address, secret):
        super().__init__(address, Handler)
        self.secret = secret
        self.slot = threading.BoundedSemaphore(1)
        self.opener = build_opener(ProxyHandler({}))


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        pass

    def reply(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def authorized(self):
        supplied = self.headers.get('Authorization', '')
        return hmac.compare_digest(supplied.encode(), ('Bearer ' + self.server.secret).encode())

    def do_GET(self):
        if not self.authorized(): return self.reply(401, {'error': 'Unauthorized'})
        if self.path != '/v1/models': return self.reply(404, {'error': 'Not found'})
        self.forward('GET')

    def do_POST(self):
        if not self.authorized(): return self.reply(401, {'error': 'Unauthorized'})
        if self.path != '/v1/chat/completions': return self.reply(404, {'error': 'Not found'})
        if self.headers.get('Transfer-Encoding') or self.headers.get('Expect'):
            return self.reply(400, {'error': 'Unsupported request'})
        try:
            length = int(self.headers.get('Content-Length', ''))
            if not 0 < length <= MAX_BODY: return self.reply(413, {'error':'Request too large'})
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or payload.get('model') != MODEL or payload.get('stream', False) is not False:
                return self.reply(400, {'error':'Unsupported model or streaming mode'})
            if not isinstance(payload.get('messages'), list) or not 1 <= len(payload['messages']) <= 30:
                return self.reply(400, {'error':'Invalid messages'})
            limit = payload.get('max_tokens', 512)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1024:
                return self.reply(400, {'error':'Invalid output limit'})
            allowed = {'model','messages','max_tokens','temperature','top_p','stream'}
            if set(payload) - allowed: return self.reply(400, {'error':'Unsupported parameter'})
        except (ValueError, TypeError, TimeoutError):
            return self.reply(400, {'error':'Invalid request'})
        self.forward('POST', json.dumps(payload).encode())

    def forward(self, method, data=None):
        if not self.server.slot.acquire(blocking=False): return self.reply(503, {'error':'Model busy'})
        try:
            request = Request(BACKEND+self.path, data=data, method=method, headers={'Content-Type':'application/json'})
            with self.server.opener.open(request, timeout=110) as response:
                body = response.read(MAX_REPLY+1)
            if len(body)>MAX_REPLY: return self.reply(502, {'error':'Response too large'})
            self.reply(200,json.loads(body))
        except (HTTPError, URLError, TimeoutError, ValueError, OSError):
            self.reply(502, {'error':'Local model unavailable or returned an invalid response'})
        finally:
            self.server.slot.release()


def main():
    secret = os.environ.get('SAKSHAM_BIONIC_BRIDGE_KEY', '')
    bind = os.environ.get('SAKSHAM_BIONIC_BIND', '')
    if len(secret)<32 or not secret.isascii() or not secret.isprintable():
        raise SystemExit('Set SAKSHAM_BIONIC_BRIDGE_KEY to an ASCII secret of at least 32 characters.')
    try:
        ip = ipaddress.ip_address(bind)
        if ip.version != 4 or ip not in ipaddress.ip_network('100.64.0.0/10'):
            raise ValueError()
    except ValueError:
        raise SystemExit('Set SAKSHAM_BIONIC_BIND to this Mac’s Tailscale IPv4 address (100.x.y.z).')
    server = Server((bind,1236),secret)
    print(f'Private bridge listening on {bind}:1236', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
