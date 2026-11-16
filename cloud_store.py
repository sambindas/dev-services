"""Durable account, pairing and command storage. SQLite is for local tests only."""
from contextlib import contextmanager
import hashlib
import json
import os
import secrets
import sqlite3
import time


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, url):
        self.url = url
        self.sqlite = url.startswith('sqlite:///')
        if self.sqlite and os.environ.get('RENDER'):
            raise RuntimeError('Hosted mode requires PostgreSQL, not temporary local storage.')
        if not self.sqlite and not url.startswith(('postgresql://', 'postgres://')):
            raise RuntimeError('Set DATABASE_URL to a PostgreSQL connection URL.')
        with self.transaction() as db:
            for sql in (
                'CREATE TABLE IF NOT EXISTS cloud_users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS cloud_sessions (token TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires DOUBLE PRECISION NOT NULL)',
                'CREATE TABLE IF NOT EXISTS cloud_pairings (token TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires DOUBLE PRECISION NOT NULL)',
                "CREATE TABLE IF NOT EXISTS cloud_agents (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, token TEXT UNIQUE NOT NULL, seen DOUBLE PRECISION NOT NULL, status TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)",
                "CREATE TABLE IF NOT EXISTS cloud_commands (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, operation TEXT NOT NULL, payload TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, state TEXT NOT NULL, result TEXT)",
                'CREATE INDEX IF NOT EXISTS cloud_command_agent ON cloud_commands(agent_id, state, created)',
                'CREATE INDEX IF NOT EXISTS cloud_agent_owner ON cloud_agents(user_id)',
            ):
                db.execute(sql)

    @contextmanager
    def transaction(self):
        if self.sqlite:
            conn = sqlite3.connect(self.url[len('sqlite:///'):], timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute('BEGIN IMMEDIATE')
        else:
            import psycopg
            from psycopg.rows import dict_row
            conn = psycopg.connect(self.url, row_factory=dict_row, connect_timeout=10)
        class DB:
            def execute(inner, sql, args=()):
                return conn.execute(sql if self.sqlite else sql.replace('?', '%s'), args)
        try:
            yield DB()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def session(self, token):
        with self.transaction() as db:
            row = db.execute('SELECT user_id FROM cloud_sessions WHERE token=? AND expires>?', (digest(token), time.time())).fetchone()
            return row['user_id'] if row else None

    def owned_agent(self, user, agent_id):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM cloud_agents WHERE id=? AND user_id=? AND revoked=0', (agent_id, user)).fetchone()
            return dict(row) if row else None

    def agent(self, token):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM cloud_agents WHERE token=? AND revoked=0', (digest(token),)).fetchone()
            return dict(row) if row else None

    def enqueue(self, agent, operation, payload):
        ident = secrets.token_hex(16)
        with self.transaction() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM cloud_commands WHERE agent_id=? AND state='queued' AND created>?", (agent, time.time()-30)).fetchone()['n']
            if count >= 20:
                raise ValueError('Agent command queue is full. Wait before trying again.')
            db.execute('INSERT INTO cloud_commands VALUES (?,?,?,?,?,?,?)', (ident, agent, operation, json.dumps(payload), time.time(), 'queued', None))
        return ident

    def poll(self, agent, status, results):
        now = time.time()
        with self.transaction() as db:
            row = db.execute('SELECT id FROM cloud_agents WHERE id=? AND revoked=0' + ('' if self.sqlite else ' FOR UPDATE'), (agent,)).fetchone()
            if not row:
                raise ValueError('Agent pairing was revoked.')
            db.execute('UPDATE cloud_agents SET seen=?,status=? WHERE id=? AND revoked=0', (now, json.dumps(status), agent))
            for result in results[:20]:
                db.execute("UPDATE cloud_commands SET state='done',result=? WHERE id=? AND agent_id=? AND state='delivered'", (json.dumps(result.get('result', {})), result.get('id', ''), agent))
            # Deliver at most once. A lost response is reported as uncertain, never replayed.
            rows = db.execute("UPDATE cloud_commands SET state='delivered' WHERE state='queued' AND id IN (SELECT id FROM cloud_commands WHERE agent_id=? AND state='queued' AND created>? ORDER BY created LIMIT 10) RETURNING id,operation,payload", (agent, now-30)).fetchall()
            db.execute('DELETE FROM cloud_commands WHERE created<?', (now-86400,))
            db.execute('DELETE FROM cloud_pairings WHERE expires<?', (now,))
            db.execute('DELETE FROM cloud_sessions WHERE expires<?', (now,))
        return [{'id': r['id'], 'operation': r['operation'], 'payload': json.loads(r['payload'])} for r in rows]

    def result(self, agent, ident):
        with self.transaction() as db:
            row = db.execute('SELECT state,result,created FROM cloud_commands WHERE id=? AND agent_id=?', (ident, agent)).fetchone()
            if not row:
                return None
            return {'state': row['state'], 'result': json.loads(row['result']) if row['result'] else None, 'created': row['created']}
