"""Opt-in outbound agent for the hosted dashboard. No incoming network port."""
import argparse
import getpass
import json
import os
from pathlib import Path
import socket
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlparse, quote
from urllib.request import Request, build_opener, ProxyHandler


def save_state(path, state):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.agent-')
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(state,stream)
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def request(origin, path, body, token=None):
    headers = {'Content-Type':'application/json'}
    if token: headers['Authorization'] = 'Bearer '+token
    # Do not send agent credentials through a proxy inherited from the environment.
    opener = build_opener(ProxyHandler({}))
    with opener.open(Request(origin+path, data=json.dumps(body).encode(), headers=headers), timeout=30) as response:
        return json.load(response)


class LocalController:
    """Use the same controller as the local UI; never race its process records."""
    def __init__(self, origin):
        parsed=urlparse(origin)
        if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost') or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('Local dashboard URL must be an HTTP loopback origin without a trailing slash.')
        self.origin=origin
        if self.call('/api/health').get('app')!='dev-services':
            raise ValueError('The selected local port is not a Dev Services dashboard.')

    def call(self,path,body=None):
        headers={'X-Dashboard':'1','Content-Type':'application/json','Origin':self.origin}
        opener=build_opener(ProxyHandler({}))
        req=Request(self.origin+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        try:
            with opener.open(req,timeout=20) as response:return json.load(response)
        except HTTPError as error:
            try:message=json.load(error).get('error','Local dashboard request failed.')
            except ValueError:message='Local dashboard request failed.'
            raise ValueError(message) from None

    def status(self):return self.call('/api/status')
    def load_config(self):return self.call('/api/services')
    def perform(self,operation,payload):
        if operation=='logs':return self.call('/api/logs/snapshot?id='+quote(payload['id'],safe=''))
        return self.call('/api/'+operation,payload)


def execute(controller, operation, payload):
    if not isinstance(payload,dict): raise ValueError('Invalid command.')
    services = controller.load_config()['services']
    if operation in ('start-all','stop-all'):
        return controller.perform(operation,{})
    svc = next((s for s in services if s['id']==payload.get('id') and not s.get('planned')),None)
    if svc is None: raise ValueError('Unknown locally configured service.')
    if operation=='logs':
        return controller.perform('logs',{'id':svc['id']})
    if operation!='action' or payload.get('action') not in ('start','stop','restart'):
        raise ValueError('Remote commands are limited to start, stop, restart and logs.')
    return controller.perform('action',{'id':svc['id'],'action':payload['action']})


def cycle(controller, state, state_path):
    snapshot=controller.status()
    # Folder paths and start commands remain on the local machine.
    snapshot['services']=snapshot['services'][:200]
    for row in snapshot['services']:
        row.pop('pull',None)
        row['canPull']=False
        row['editable']=False
        for field in ('name','notes','detail'):
            if isinstance(row.get(field),str): row[field]=row[field][:500]
    journal=state.setdefault('journal',{})
    results=[{'id':ident,'result':value} for ident,value in list(journal.items())[-10:]]
    response=request(state['origin'],'/agent/poll',{'status':snapshot,'results':results},state['token'])
    for command in response.get('commands',[]):
        ident=command['id']
        if ident in journal: continue
        # Record before execution. A crash leaves an uncertain result, never a replay.
        journal[ident]={'error':'Agent interrupted; action outcome is uncertain. Check service status.'}
        save_state(state_path,state)
        try:
            journal[ident]=execute(controller,command['operation'],command['payload'])
        except Exception as error:
            journal[ident]={'error':str(error)[:300]}
        while len(journal)>100: journal.pop(next(iter(journal)))
        save_state(state_path,state)


def main():
    parser=argparse.ArgumentParser(description='Pair your local Dev Services projects with a hosted dashboard.')
    parser.add_argument('--pair',metavar='HTTPS_URL',help='Pair with a hosted dashboard; prompts for a one-time code')
    parser.add_argument('--disconnect',action='store_true',help='Remove this computer\'s local pairing credential')
    parser.add_argument('--local-url',default='http://127.0.0.1:7777',help='Existing local dashboard origin')
    args=parser.parse_args()
    state_path=Path(os.environ.get('AGENT_DATA_DIR') or Path(__file__).resolve().parent)/'remote-agent.json'
    if args.disconnect:
        state_path.unlink(missing_ok=True)
        print('Local pairing removed. Stop any running agent and revoke the computer in the web dashboard.')
        return
    if args.pair:
        parsed=urlparse(args.pair)
        if parsed.scheme!='https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
            parser.error('Use the exact HTTPS dashboard origin, without a trailing slash.')
        controller=LocalController(args.local_url)
        print('Pairing grants your account remote start/stop/restart access to projects in '+args.local_url+'.')
        print('Service names, status and requested logs are sent to the hosted dashboard. Commands and folders stay local.')
        if input('Type PAIR to continue: ').strip()!='PAIR': return
        code=getpass.getpass('One-time pairing code: ').strip()
        result=request(args.pair,'/agent/pair',{'code':code,'name':socket.gethostname()[:80]})
        state={'origin':args.pair,'token':result['token'],'id':result['id'],'local_url':args.local_url,'journal':{}}
        save_state(state_path,state)
        print('Paired. This agent needs to remain running for remote controls to work.')
    else:
        if not state_path.exists(): parser.error('Not paired. Run agent.py --pair https://your-dashboard-host')
        state=json.loads(state_path.read_text())
        controller=LocalController(state.get('local_url',args.local_url))
    print('Agent connected. Press Ctrl+C to disconnect; your projects will keep running.')
    failures=0
    try:
        while state_path.exists():
            try:
                cycle(controller,state,state_path)
                if failures: print('Connection restored.')
                failures=0
                time.sleep(3)
            except HTTPError as error:
                if error.code==401:
                    print('Pairing was revoked. Pair again to reconnect.'); return
                failures+=1
                if failures==1: print('Dashboard unavailable. Retrying; projects keep running locally.')
                time.sleep(min(60,3*2**min(failures,4)))
            except (OSError,ValueError):
                failures+=1
                if failures==1: print('Connection interrupted. Retrying; projects keep running locally.')
                time.sleep(min(60,3*2**min(failures,4)))
    except KeyboardInterrupt:
        print('\nAgent stopped. Project processes were left running.')


if __name__=='__main__': main()
