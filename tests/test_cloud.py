import hashlib
from http.cookies import SimpleCookie
import http.client
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cloud_server import CloudServer
from cloud_store import Store, digest
import agent


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.url=os.environ.get('CLOUD_TEST_DATABASE_URL') or 'sqlite:///'+str(Path(self.temp.name)/'cloud.sqlite3')
        if not self.url.startswith('sqlite:'):
            from urllib.parse import urlparse
            if not urlparse(self.url).path.startswith('/dev_services_test_'):
                raise RuntimeError('Postgres tests require an isolated dev_services_test_ database.')
        self.store=Store(self.url)
        self.server=CloudServer(('127.0.0.1',0),self.store,'http://127.0.0.1')
        self.port=self.server.server_address[1]
        self.origin=f'http://127.0.0.1:{self.port}'
        self.server.origin=self.origin
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
        if not self.store.sqlite:
            with self.store.transaction() as db:
                for table in ('cloud_commands','cloud_agents','cloud_pairings','cloud_sessions','cloud_users'):
                    db.execute('DELETE FROM '+table)
        self.temp.cleanup()

    def request(self,path,body=None,cookie=None,token=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=10)
        hdr={'X-Dashboard':'1','Content-Type':'application/json','Origin':self.origin}
        if cookie:hdr['Cookie']=cookie
        if token:hdr['Authorization']='Bearer '+token
        hdr.update(headers or {})
        conn.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,hdr)
        response=conn.getresponse()
        data=response.read();result=(response.status,dict(response.getheaders()),data)
        conn.close()
        return result

    def register(self,name):
        status,headers,_=self.request('/api/register',{'username':name,'password':'a test password at least 12 chars'})
        self.assertEqual(status,200)
        return headers['Set-Cookie'].split(';')[0]

    def pair(self,cookie):
        response=self.request('/api/cloud/pair',{},cookie)
        code=json.loads(response[2])['code']
        status,_,data=self.request('/agent/pair',{'code':code,'name':'Test computer'})
        self.assertEqual(status,200)
        result=json.loads(data)
        return code,result['id'],result['token']

    def publish(self,token,results=None):
        status={'groups':['Projects'],'services':[{'id':'api','name':'API','type':'process','running':False,'managed':False,'port':32100}], 'bulk':None,'now':time.time()}
        response=self.request('/agent/poll',{'status':status,'results':results or []},token=token)
        self.assertEqual(response[0],200)
        return json.loads(response[2])

    def test_auth_cookie_password_hashing_logout_and_origin(self):
        self.assertEqual(self.request('/')[0],303)
        self.assertEqual(self.request('/api/cloud/agents')[0],401)
        self.assertEqual(self.request('/auth')[0],200)
        cookie=self.register('alice')
        self.assertEqual(self.request('/',cookie=cookie)[0],200)
        self.assertEqual(self.request('/api/cloud/pair',{},cookie,headers={'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('/api/cloud/pair',{},cookie,headers={'X-Dashboard':''})[0],403)
        self.assertEqual(self.request('/api/login',{'username':'alice','password':'incorrect password long'})[0],401)
        with self.store.transaction() as db:
            user=db.execute('SELECT password FROM cloud_users').fetchone()
            self.assertNotIn('a test password',user['password'])
            session=db.execute('SELECT token FROM cloud_sessions').fetchone()
            self.assertNotIn(session['token'],cookie)
        self.assertEqual(self.request('/api/logout',{},cookie)[0],200)
        self.assertEqual(self.request('/api/cloud/agents',cookie=cookie)[0],401)

    def test_tenant_isolation_pairing_once_revocation_and_persistence(self):
        alice=self.register('alice');bob=self.register('bob')
        code,ident,token=self.pair(alice)
        self.assertEqual(self.request('/agent/pair',{'code':code,'name':'Replay'})[0],401)
        self.publish(token)
        self.assertEqual(self.request('/api/status?agent='+ident,cookie=alice)[0],200)
        self.assertEqual(self.request('/api/status?agent='+ident,cookie=bob)[0],404)
        self.assertEqual(self.request('/api/action?agent='+ident,{'id':'api','action':'start'},bob)[0],404)
        restored=Store(self.url)
        self.assertIsNotNone(restored.agent(token))
        with self.store.transaction() as db:
            self.assertNotEqual(token,db.execute('SELECT token FROM cloud_agents WHERE id=?',(ident,)).fetchone()['token'])
        self.request('/api/cloud/revoke',{'id':ident},bob)
        self.assertIsNotNone(self.store.agent(token))
        self.request('/api/cloud/revoke',{'id':ident},alice)
        self.assertEqual(self.request('/agent/poll',{'status':{'services':[]},'results':[]},token=token)[0],401)

    def test_commands_expire_and_are_not_redelivered(self):
        alice=self.register('alice');_,ident,token=self.pair(alice)
        self.publish(token)
        response=self.request('/api/action?agent='+ident,{'id':'api','action':'start'},alice)
        self.assertEqual(response[0],202)
        command=json.loads(response[2])['commandId']
        first=self.publish(token)['commands']
        self.assertEqual(first[0]['id'],command)
        self.assertEqual(self.publish(token)['commands'],[])
        self.publish(token,[{'id':command,'result':{'ok':True}}])
        result=self.request('/api/cloud/result?agent='+ident+'&id='+command,cookie=alice)
        self.assertEqual(json.loads(result[2])['state'],'done')
        old=self.store.enqueue(ident,'action',{'id':'api','action':'stop'})
        with self.store.transaction() as db:db.execute('UPDATE cloud_commands SET created=? WHERE id=?',(time.time()-31,old))
        self.assertEqual(self.publish(token)['commands'],[])
        self.assertEqual(self.request('/api/action?agent='+ident,{'id':'api','action':'pull'},alice)[0],400)
        self.assertEqual(self.request('/api/action?agent='+ident,{'id':'unknown','action':'start'},alice)[0],400)

    def test_pairing_expiry_offline_commands_and_download(self):
        alice=self.register('alice')
        response=self.request('/api/cloud/pair',{},alice)
        code=json.loads(response[2])['code']
        with self.store.transaction() as db:db.execute('UPDATE cloud_pairings SET expires=?',(time.time()-1,))
        self.assertEqual(self.request('/agent/pair',{'code':code,'name':'Expired'})[0],401)
        _,ident,token=self.pair(alice)
        self.assertEqual(self.request('/api/action?agent='+ident,{'id':'api','action':'start'},alice)[0],409)
        response=self.request('/downloads/agent.zip',cookie=alice)
        self.assertEqual(response[0],200)
        import io,zipfile
        with zipfile.ZipFile(io.BytesIO(response[2])) as bundle:
            names=bundle.namelist()
            self.assertIn('dev-services-agent/agent.py',names)
            self.assertFalse(any('services.json' in name or 'remote-agent.json' in name for name in names))

    def test_agent_rejects_remote_configuration_and_saves_secret_privately(self):
        controller=MagicMock()
        controller.load_config.return_value={'services':[{'id':'api','type':'process'}]}
        for operation,payload in [('shell',{'cmd':'touch /tmp/no'}),('action',{'id':'api','action':'pull'}),('action',{'id':'unknown','action':'start'})]:
            with self.assertRaises(ValueError):agent.execute(controller,operation,payload)
        controller.action_thread.assert_not_called()
        path=Path(self.temp.name)/'agent.json'
        agent.save_state(path,{'token':'test'})
        if os.name!='nt':self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_real_agent_starts_restarts_and_stops_disposable_project(self):
        folder=Path(self.temp.name)/'local';folder.mkdir()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        with patch.dict(os.environ,{'DASHBOARD_DATA_DIR':str(folder)}):
            spec=importlib.util.spec_from_file_location('cloud_test_local_controller',ROOT/'server.py')
            controller=importlib.util.module_from_spec(spec);spec.loader.exec_module(controller)
        svc={'id':'api','name':'Disposable API','group':'Projects','type':'process','cwd':str(folder),'cmd':f'"{sys.executable}" -u -m http.server {port} --bind 127.0.0.1','port':port,'startTimeout':5}
        controller.service_config.save(controller.CONFIG_FILE,{'services':[svc],'groups':['Projects']})
        local_server=controller.ThreadingHTTPServer(('127.0.0.1',0),controller.Handler)
        controller.PORT=local_server.server_address[1]
        local_thread=threading.Thread(target=local_server.serve_forever,daemon=True)
        local_thread.start()
        local=agent.LocalController(f'http://127.0.0.1:{controller.PORT}')
        alice=self.register('alice');_,ident,token=self.pair(alice)
        state={'origin':self.origin,'id':ident,'token':token,'journal':{}}
        path=folder/'remote-agent.json'
        def cycle():agent.cycle(local,state,path)
        def wait(condition):
            end=time.monotonic()+8
            while time.monotonic()<end:
                if condition():return
                time.sleep(.05)
            self.fail('Disposable service did not reach expected state')
        try:
            cycle()
            for action in ('start','restart','stop'):
                response=self.request('/api/action?agent='+ident,{'id':'api','action':action},alice)
                self.assertEqual(response[0],202)
                cycle()
                wait(lambda: controller.busy.get('api',{}).get('done') or 'api' not in controller.busy)
                self.assertNotIn('error',controller.busy.get('api',{}))
                cycle()
                self.assertEqual(controller.port_open(port),action!='stop')
            text=agent.execute(local,'logs',{'id':'api'})['text']
            self.assertIn('started',text)
        finally:
            controller.processes.stop(svc)
            local_server.shutdown();local_server.server_close();local_thread.join()

    def test_agent_status_cannot_inject_html_into_uptime_or_links(self):
        alice=self.register('alice');_,ident,token=self.pair(alice)
        status={'services':[{'id':'api','name':'API','since':'\" onclick=alert(1)','url':'javascript:alert(1)'}]}
        self.assertEqual(self.request('/agent/poll',{'status':status},token=token)[0],200)
        response=self.request('/api/status?agent='+ident,cookie=alice)
        row=json.loads(response[2])['services'][0]
        self.assertIsNone(row['since']);self.assertIsNone(row['url'])


if __name__=='__main__':unittest.main()
