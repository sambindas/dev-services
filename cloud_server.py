"""Hosted relay. Deliberately never imports the local process controller."""
import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import urlparse, parse_qs
import zipfile

from cloud_store import Store, digest

ROOT = Path(__file__).resolve().parent
COOKIE = '__Host-devservices'


class Problem(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + ':' + hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def valid_password(password, encoded):
    return hmac.compare_digest(hash_password(password, encoded.split(':')[0]), encoded)


def clean_status(status):
    """The browser renders only known, typed status fields from each agent."""
    def number(value):
        return value if type(value) in (float,int) and math.isfinite(value) and value >= 0 else None
    services=[]
    for raw in status['services']:
        if not isinstance(raw,dict) or not isinstance(raw.get('id'),str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}',raw['id']):
            raise Problem(400,'Invalid service status.')
        row={key:str(raw.get(key) or '')[:2000] for key in ('id','name','group','type','notes','detail')}
        for key in ('running','managed','planned','unsupported','external'):
            row[key]=raw.get(key) is True
        row['port']=raw.get('port') if type(raw.get('port')) is int and 1 <= raw['port'] <= 65535 else None
        row['since']=number(raw.get('since'))
        url=raw.get('url','')
        row['url']=url if isinstance(url,str) and len(url)<=4096 and urlparse(url).scheme in ('http','https') else None
        busy=raw.get('busy')
        if isinstance(busy,dict) and busy.get('action') in ('start','stop','restart'):
            row['busy']={'action':busy['action'],'since':number(busy.get('since')) or time.time(),'done':busy.get('done') is True,'error':str(busy.get('error') or '')[:1000]}
        services.append(row)
    groups=list(dict.fromkeys(row['group'] for row in services))
    return {'services':services,'groups':groups,'now':time.time(),'bulk':status.get('bulk') if status.get('bulk') in ('start','stop') else None,'platform':str(status.get('platform',''))[:30]}


class CloudServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, store, origin):
        self.store, self.origin = store, origin
        self.attempts, self.attempt_lock = {}, threading.Lock()
        super().__init__(address, Handler)

    def limit(self, key, maximum=12):
        with self.attempt_lock:
            now = time.monotonic()
            self.attempts = {k: v for k, v in self.attempts.items() if v[0] > now-60}
            start, count = self.attempts.get(key, (now, 0))
            if count >= maximum or len(self.attempts) > 10000:
                raise Problem(429, 'Too many attempts. Please wait a minute.')
            self.attempts[key] = (start, count+1)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Never log cookies, pairing codes, agent tokens or database URLs.

    def send(self, status, body, ctype='application/json', headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        for key, value in {'Content-Type': ctype, 'Content-Length': str(len(data)), 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'same-origin', 'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'", **(headers or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        size = int(self.headers.get('Content-Length', 0))
        if not 0 < size <= 524288:
            raise Problem(413, 'Request size must be between 1 byte and 512 KB.')
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise Problem(400, 'Expected a JSON object.')
        return value

    def session_token(self):
        cookie = SimpleCookie()
        cookie.load(self.headers.get('Cookie', ''))
        name = COOKIE if self.server.origin.startswith('https:') else 'devservices-localtest'
        return cookie[name].value if name in cookie else ''

    def user(self):
        user = self.server.store.session(self.session_token())
        if not user:
            raise Problem(401, 'Please sign in.')
        return user

    def owned(self, user, query):
        agent = self.server.store.owned_agent(user, query.get('agent', [''])[0])
        if not agent:
            raise Problem(404, 'Select one of your paired computers.')
        return agent

    def dispatch(self, post=False):
        try:
            origin = self.server.origin
            if self.headers.get('Host') != urlparse(origin).netloc or (self.headers.get('Origin') and self.headers['Origin'] != origin):
                raise Problem(403, 'Request origin is not allowed.')
            path = urlparse(self.path)
            query = parse_qs(path.query)
            if post and not path.path.startswith('/agent/') and self.headers.get('X-Dashboard') != '1':
                raise Problem(403, 'Request header is required.')
            return self.route(path.path, query, self.read_json() if post else None)
        except Problem as error:
            if error.status == 401 and urlparse(self.path).path == '/':
                return self.send(303, b'', headers={'Location':'/auth'})
            self.send(error.status, {'error': error.message})
        except (ValueError, TypeError, KeyError):
            self.send(400, {'error': 'Invalid request.'})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.send(503, {'error': 'Service temporarily unavailable. Please retry.'})

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch(True)

    def route(self, path, query, body):
        store = self.server.store
        if path == '/api/health' and body is None:
            with store.transaction() as db:
                db.execute('SELECT 1')
            return self.send(200, {'ok': True, 'app': 'dev-services-cloud'})
        if path == '/auth' and body is None:
            return self.send(200, (ROOT/'cloud_auth.html').read_bytes(), 'text/html; charset=utf-8')
        if path in ('/api/register', '/api/login') and body is not None:
            self.server.limit(('auth', self.client_address[0]), 30)
            username, password = body.get('username', ''), body.get('password', '')
            if not isinstance(username, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{3,32}', username) or not isinstance(password, str) or not 12 <= len(password) <= 256:
                raise Problem(400, 'Use a username of 3–32 letters, digits, underscores or hyphens, and a password of 12–256 characters.')
            username = username.lower()
            self.server.limit(('username', username))
            with store.transaction() as db:
                row = db.execute('SELECT * FROM cloud_users WHERE username=?', (username,)).fetchone()
                if path == '/api/register':
                    if row:
                        raise Problem(409, 'That username is unavailable.')
                    user = secrets.token_hex(16)
                    db.execute('INSERT INTO cloud_users VALUES (?,?,?)', (user, username, hash_password(password)))
                else:
                    if not row or not valid_password(password, row['password']):
                        raise Problem(401, 'Incorrect username or password.')
                    user = row['id']
                token = secrets.token_urlsafe(32)
                db.execute('INSERT INTO cloud_sessions VALUES (?,?,?)', (digest(token), user, time.time()+7*86400))
            secure = '; Secure' if self.server.origin.startswith('https:') else ''
            name = COOKIE if secure else 'devservices-localtest'
            return self.send(200, {'ok': True}, headers={'Set-Cookie': f'{name}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800{secure}'})
        if path == '/agent/pair' and body is not None:
            self.server.limit(('pair', self.client_address[0]), 20)
            code, name = body.get('code', ''), body.get('name', '')
            if not isinstance(code, str) or len(code) > 100 or not isinstance(name, str) or not 1 <= len(name) <= 80:
                raise Problem(400, 'Invalid pairing details.')
            with store.transaction() as db:
                pairing = db.execute('DELETE FROM cloud_pairings WHERE token=? AND expires>? RETURNING user_id', (digest(code), time.time())).fetchone()
                if not pairing:
                    raise Problem(401, 'Pairing code is invalid, expired, or already used.')
                if db.execute('SELECT COUNT(*) AS n FROM cloud_agents WHERE user_id=? AND revoked=0', (pairing['user_id'],)).fetchone()['n'] >= 10:
                    raise Problem(400, 'Maximum of 10 paired computers per account.')
                ident, token = secrets.token_hex(16), secrets.token_urlsafe(32)
                db.execute('INSERT INTO cloud_agents VALUES (?,?,?,?,?,?,0)', (ident, pairing['user_id'], name, digest(token), 0, '{}'))
            return self.send(200, {'id': ident, 'token': token})
        if path == '/agent/poll' and body is not None:
            agent = store.agent(self.headers.get('Authorization', '').removeprefix('Bearer '))
            if not agent:
                raise Problem(401, 'Agent is revoked or not paired.')
            status, results = body.get('status'), body.get('results', [])
            if not isinstance(status, dict) or not isinstance(status.get('services'), list) or len(status['services']) > 200 or not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
                raise Problem(400, 'Invalid agent update.')
            return self.send(200, {'commands': store.poll(agent['id'], clean_status(status), results)})
        user = self.user()
        if path == '/api/logout' and body is not None:
            with store.transaction() as db:
                db.execute('DELETE FROM cloud_sessions WHERE token=?', (digest(self.session_token()),))
            name = COOKIE if self.server.origin.startswith('https:') else 'devservices-localtest'
            return self.send(200, {'ok': True}, headers={'Set-Cookie': f'{name}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0' + ('; Secure' if self.server.origin.startswith('https:') else '')})
        if path == '/' and body is None:
            page = (ROOT/'index.html').read_text()
            page = page.replace('<script>', '<script src="/cloud.js"></script>\n<script>', 1)
            page = page.replace('Local workspace', 'Paired workspace').replace('Services run on this computer', 'Services run on your paired computer')
            return self.send(200, page.encode(), 'text/html; charset=utf-8')
        if path == '/cloud.js' and body is None:
            return self.send(200, (ROOT/'cloud.js').read_bytes(), 'text/javascript; charset=utf-8')
        if path == '/downloads/agent.zip' and body is None:
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
                for name in ('agent.py', 'server.py', 'platform_runtime.py', 'service_config.py', 'git_ops.py', 'index.html', 'requirements.txt', 'AGENT.md'):
                    bundle.write(ROOT/name, 'dev-services-agent/'+name)
            return self.send(200, archive.getvalue(), 'application/zip', {'Content-Disposition':'attachment; filename="dev-services-agent.zip"'})
        if path == '/api/cloud/agents' and body is None:
            with store.transaction() as db:
                rows = db.execute('SELECT id,name,seen FROM cloud_agents WHERE user_id=? AND revoked=0 ORDER BY name', (user,)).fetchall()
            return self.send(200, {'agents': [dict(row) for row in rows]})
        if path == '/api/cloud/pair' and body is not None:
            self.server.limit(('new-pair', user), 10)
            code = secrets.token_urlsafe(32)
            with store.transaction() as db:
                db.execute('INSERT INTO cloud_pairings VALUES (?,?,?)', (digest(code), user, time.time()+600))
            return self.send(200, {'code': code, 'origin': self.server.origin, 'expiresIn':600})
        if path == '/api/cloud/revoke' and body is not None:
            with store.transaction() as db:
                db.execute('UPDATE cloud_agents SET revoked=1 WHERE id=? AND user_id=?', (body.get('id'), user))
            return self.send(200, {'ok': True})
        if path == '/api/status' and body is None and not query.get('agent'):
            return self.send(200, {'groups':[], 'services':[], 'now':time.time(), 'bulk':None, 'platform':'cloud'})
        agent = self.owned(user, query)
        status = json.loads(agent['status'])
        online = agent['seen'] > time.time()-20
        if path == '/api/status' and body is None:
            for row in status.get('services', []):
                row['editable'], row['canPull'], row['pull'] = False, False, None
                if not online:
                    row.update(unsupported=True, detail='Agent offline. Reconnect it on this computer.')
            return self.send(200, {**status, 'now':time.time(), 'groups':status.get('groups',[]), 'services':status.get('services',[]), 'agentOnline':online})
        if path == '/api/cloud/result' and body is None:
            result = store.result(agent['id'], query.get('id',[''])[0])
            if not result:
                raise Problem(404, 'Command not found.')
            return self.send(200, result)
        if not online:
            raise Problem(409, 'Agent is offline. Start it on your computer first.')
        if path == '/api/action' and body is not None:
            if body.get('action') not in ('start','stop','restart') or not any(s.get('id') == body.get('id') and not s.get('planned') and not s.get('unsupported') and not s.get('external') for s in status.get('services',[])):
                raise Problem(400, 'Select a configured service and a supported action.')
            ident = store.enqueue(agent['id'], 'action', {'id':body['id'],'action':body['action']})
            return self.send(202, {'ok':True, 'commandId':ident})
        if path in ('/api/start-all','/api/stop-all') and body is not None:
            ident = store.enqueue(agent['id'], path.split('/')[-1], {})
            return self.send(202, {'ok':True, 'commandId':ident})
        if path == '/api/logs/stream' and body is None:
            sid = query.get('id',[''])[0]
            if not any(s.get('id') == sid for s in status.get('services',[])):
                raise Problem(404, 'Service not found.')
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            previous = None
            for _ in range(120):
                if store.session(self.session_token()) != user or not store.owned_agent(user,agent['id']):
                    break
                ident = store.enqueue(agent['id'], 'logs', {'id':sid})
                result = None
                for _ in range(80):
                    row = store.result(agent['id'],ident)
                    if row and row['state']=='done':
                        result = row['result']; break
                    time.sleep(.25)
                if result is None:
                    result = {'error':'Agent did not respond. Reconnect the agent and reopen logs.'}
                text = result.get('text','')
                event = {'lines':text.splitlines(), 'replace':True} if not result.get('error') else {'note':result['error'], 'done':True}
                if text != previous or result.get('error'):
                    self.wfile.write(b'data: '+json.dumps(event).encode()+b'\n\n')
                    previous = text
                self.wfile.write(b': ping\n\n'); self.wfile.flush()
                if result.get('error'): break
                time.sleep(3)
            return
        raise Problem(404, 'This action is available in the local dashboard only.')


def main():
    origin = os.environ.get('PUBLIC_ORIGIN') or os.environ.get('RENDER_EXTERNAL_URL','')
    if not origin.startswith('https://') or urlparse(origin).path:
        raise RuntimeError('PUBLIC_ORIGIN must be the exact HTTPS origin without a trailing slash.')
    store = Store(os.environ.get('DATABASE_URL',''))
    print('Dev Services cloud relay starting', flush=True)
    CloudServer(('0.0.0.0', int(os.environ.get('PORT','10000'))), store, origin).serve_forever()


if __name__ == '__main__':
    main()
