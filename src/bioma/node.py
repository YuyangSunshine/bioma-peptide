"""Independent scientific Harness node with its own durable execution store.

Development: python -m bioma.node --store .bioma/node-a --peer calculator-a --port 8021 --local-insecure
"""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from bioma.runtime.participant import RuntimeParticipant
from bioma.backends.sequence import sequence_backend
from bioma.core.models import CapabilitySpec
from bioma.runtime.federation import NodeJournal
from bioma.runtime.ledger import RunLedger
from bioma.runtime.projections import LocalAgentView
from bioma.runtime.node_client import NodeClient, loopback
from bioma.runtime.security import TrustStore, digest
from bioma.runtime.artifact_store import ArtifactStore


def request_json(url, payload=None):
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if not loopback(parsed.hostname or ''):
        raise PermissionError('Use NodeClient with pinned identities for remote nodes')
    request = Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                      headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=90) as response:
        return json.load(response)


class RemoteAgent(RuntimeParticipant):
    def __init__(self, endpoint, *, client=None, local_insecure=False, cache_dir=None, export_roots=(), observation_trust=None):
        self.endpoint = endpoint.rstrip('/')
        self.client = client or NodeClient(endpoint, local_insecure=local_insecure)
        if self.client.endpoint != self.endpoint:
            raise ValueError('RemoteAgent and authenticated client endpoints differ')
        self.observation_trust = observation_trust or TrustStore()
        self.secure = self.client.identity is not None
        cache = cache_dir or Path('.bioma') / 'remote-artifacts'
        self.artifact_store = ArtifactStore(cache, allowed_roots=(*export_roots, cache))
        catalog = self.client.request('/capabilities')
        super().__init__(catalog['peer_ids'])
        self.capabilities = [CapabilitySpec.model_validate(c) for c in catalog['capabilities']]
        if any(c.agent_id not in self.peer_ids for c in self.capabilities):
            raise ValueError('Remote catalog exposes an undeclared peer identity')

    def parallel_safe(self, state, message):
        return True

    def confirm_commit(self, event):
        from bioma.core.models import StateDelta
        from bioma.core.transitions import execution_identity
        owner = StateDelta.model_validate(event.payload['delta']['transition']['owner_delta'])
        return self.client.request('/commit', {'run_id':event.run_id,
            'message_id':event.payload['message']['id'], 'event_id':event.event_id,
            'event_hash':event.event_hash, 'execution_identity':execution_identity(owner)})

    def prepare(self, state, message):
        raise RuntimeError("Independent remote execution is BLOCKED: server MethodBinding identity and WP1 authority are not attested")

    def handle(self, state, message):
        raise RuntimeError('RemoteAgent production execution is BLOCKED: direct-backend provenance is not established')


class NodeServer(ThreadingHTTPServer):
    def __init__(self, address, provider, store, peers=(), *, identity=None, trust=None,
                 local_insecure=False, ssl_context=None, export_roots=(), peer_clients=None, observation_trust=None):
        if identity is None or trust is None:
            if not local_insecure or not loopback(address[0]):
                raise ValueError('Node identity and trust are required; local_insecure is loopback-only')
        elif local_insecure:
            raise ValueError('Secure nodes cannot enable local_insecure')
        if not loopback(address[0]) and ssl_context is None:
            raise ValueError('Non-loopback listeners require TLS')
        self.identity, self.trust = identity, trust
        self.peer_clients = dict(peer_clients or {})
        self.observation_trust = observation_trust or TrustStore()
        self.provider = provider
        self.ledger = RunLedger(Path(store) / 'run.sqlite3')
        from bioma.memory import MemoryService
        self.memory = MemoryService(Path(store) / 'memory.sqlite3')
        self.journal = NodeJournal(Path(store) / 'facts.sqlite3', provider.peer_ids[0])
        self.peers = tuple(peers)
        self.artifact_store = ArtifactStore(Path(store) / 'artifacts', allowed_roots=(*export_roots, Path(store) / 'artifacts'))
        with self.journal.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS assignments(message_id TEXT PRIMARY KEY, payload TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS receipt_contexts(message_id TEXT PRIMARY KEY, payload TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS auth_nonces(principal TEXT, nonce TEXT, expires REAL, PRIMARY KEY(principal,nonce))')
            db.execute('CREATE TABLE IF NOT EXISTS event_signatures(id TEXT PRIMARY KEY, envelope TEXT)')
        super().__init__(address, NodeHandler)
        if ssl_context is not None:
            self.socket = ssl_context.wrap_socket(self.socket, server_side=True)

    def replicate(self):
        for peer in self.peers:
            try:
                if self.identity:
                    client = self.peer_clients[peer]
                    def send(event):
                        self.authorize(client.server_id, 'node.receive', run_id=event['payload']['run_id'])
                        return client.request('/replicate', self.signed_event(event))
                    self.journal.flush(peer, send)
                else:
                    self.journal.flush(peer, lambda event: request_json(peer + '/replicate', event))
            except (OSError, ValueError, KeyError):
                continue

    def signed_event(self, event):
        with self.journal.connect() as db:
            old = db.execute('SELECT envelope FROM event_signatures WHERE id=?', (event['id'],)).fetchone()
            if old:
                return json.loads(old[0])
            # Never adopt and sign a foreign event lacking its origin envelope.
            if event['payload'].get('origin_node') != self.identity.key_id:
                raise PermissionError('Cannot sign an event from another node')
            envelope = self.identity.sign('bioma.node.event.v1', event)
            db.execute('INSERT INTO event_signatures VALUES(?,?)', (event['id'], json.dumps(envelope)))
            return envelope

    def authorize(self, principal, action, *, run_id=None, resource=None, agent=None):
        if self.trust:
            self.trust.require(principal, action, run_id=run_id, resource=resource, agent=agent)

    def submit(self, payload, *, principal=None):
        raise RuntimeError("Independent remote execution is BLOCKED: server MethodBinding identity and WP1 authority are not attested")

    def tick(self):
        raise RuntimeError("Independent remote execution is BLOCKED: server MethodBinding identity and WP1 authority are not attested")

    def execute_assignment(self, view, message, event, assignment):
        raise RuntimeError("Independent remote execution is BLOCKED: server MethodBinding identity and WP1 authority are not attested")

    def confirm_commit(self, payload, principal):
        """Only the assigning authority can promote this node's verified memory.

        The acknowledgement contains no other agent's state or private inputs.
        It does not make this node a global scientific-state commit authority.
        """
        self.authorize(principal, 'task.submit', run_id=payload['run_id'])
        with self.journal.connect() as db:
            row=db.execute('SELECT payload FROM assignments WHERE message_id=?',(payload['message_id'],)).fetchone()
            context=db.execute('SELECT payload FROM receipt_contexts WHERE message_id=?',(payload['message_id'],)).fetchone()
        if row is None or context is None:
            raise ValueError('Commit acknowledgement has no verified local assignment')
        assignment=json.loads(row[0]); view=LocalAgentView.model_validate_json(context[0])
        if assignment.get('principal') != principal or view.task_id != payload['run_id']:
            raise PermissionError('Only the assigning authority can acknowledge its result')
        result=next((e['payload'] for e in self.journal.projection()['facts'] if e['kind']=='DeliveryResult'
                     and e['payload']['message_id']==payload['message_id']),None)
        from bioma.core.models import StateDelta
        from bioma.core.transitions import execution_identity
        if not result or result['receipt']['decision']!='approved':
            raise ValueError('Unverified result cannot enter long-term memory')
        delta=StateDelta.model_validate(result['delta'])
        if execution_identity(delta) != payload['execution_identity']:
            raise ValueError('Commit acknowledgement changed the local scientific output')
        # Materialize this node's own content-addressed files for recall hashing.
        if self.identity:
            delta=delta.model_copy(update={'artifacts':[self.artifact_store.materialize(a,
                lambda item:self.artifact_store.read(view.task_id,item.id,item.metadata['sha256'])) for a in delta.artifacts]})
        event=self.ledger.append(view.task_id,'RuntimeCommitAcknowledged',delta.agent_id,payload,
            idempotency_key='commit-ack:'+payload['message_id'])
        from dataclasses import replace
        self.memory.consume(replace(event,kind='TransitionCommitted',payload={
            'before':view.model_dump(mode='json'),'delta':delta.model_dump(mode='json'),'receipt':result['receipt']}))
        return {'acknowledged':payload['event_id']}


class NodeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, payload, status=200):
        if self.server.identity and hasattr(self, 'nonce'):
            payload = self.server.identity.sign('bioma.node.response.v1', {
                'nonce': self.nonce, 'path': self.path, 'result': payload})
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authenticate(self, payload):
        if not self.server.identity:
            return None
        import base64
        import sqlite3
        try:
            raw = self.headers.get('X-BioMA-Auth', '')
            if len(raw) > 8192:
                raise ValueError('Oversized authentication header')
            envelope = json.loads(base64.b64decode(raw, validate=True))
            principal, claims = self.server.trust.verify(envelope, 'bioma.node.request.v1')
            if (claims.get('method') != self.command or claims.get('path') != self.path
                or claims.get('body_sha256') != digest(payload) or claims.get('audience') != self.server.identity.key_id
                or not isinstance(claims.get('timestamp'), (int, float)) or not abs(time.time() - claims['timestamp']) <= 60
                or not isinstance(claims.get('nonce'), str) or not 16 <= len(claims['nonce']) <= 128):
                raise PermissionError('Invalid request binding or expired signature')
            self.nonce = claims['nonce']
            with self.server.journal.connect() as db:
                db.execute('DELETE FROM auth_nonces WHERE expires<?', (time.time(),))
                db.execute('INSERT INTO auth_nonces VALUES(?,?,?)', (principal, self.nonce, time.time() + 120))
            return principal
        except (ValueError, TypeError, KeyError, sqlite3.IntegrityError) as exc:
            raise PermissionError('Invalid or replayed request') from exc

    def do_GET(self):
        try:
            self.principal = self.authenticate(None)
            self.get()
        except PermissionError as exc:
            self.reply({'error': str(exc)}, 403)
        except (ValueError, KeyError, TypeError) as exc:
            self.reply({'error': str(exc)}, 400)

    def get(self):
        if self.path == '/capabilities':
            self.server.authorize(self.principal, 'capabilities.read')
            self.reply({'peer_ids': self.server.provider.peer_ids,
                        'capabilities': [c.model_dump(mode='json') for c in self.server.provider.capabilities]})
        elif self.path == '/projection':
            if self.server.identity:
                raise PermissionError('Global projection is disabled; use authorized task results')
            self.reply(self.server.journal.projection())
        elif self.path.startswith('/result/'):
            key = self.path.rsplit('/', 1)[-1]
            with self.server.journal.connect() as db:
                assignment = db.execute('SELECT payload FROM assignments WHERE message_id=?', (key,)).fetchone()
            if assignment is None:
                raise PermissionError('Unknown or inaccessible assignment')
            run_id = json.loads(assignment[0])['view']['task_id']
            self.server.authorize(self.principal, 'result.read', run_id=run_id)
            result = next((e['payload'] for e in self.server.journal.projection()['facts']
                if e['kind'] in {'DeliveryResult', 'ExecutionFailure'} and e['payload']['message_id'] == key), None)
            if result and 'delta' in result:
                with self.server.journal.connect() as db:
                    row = db.execute('SELECT payload FROM receipt_contexts WHERE message_id=?', (key,)).fetchone()
                if row:
                    result = {**result, 'view': json.loads(row[0])}
            self.reply(result or {'status': 'waiting'})
        else:
            self.reply({'error': 'not found'}, 404)

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            limit = 48 * 1024 * 1024 if self.path == '/artifact/upload' else 4 * 1024 * 1024
            if not 0 < length <= limit:
                raise ValueError('Invalid request size')
            payload = json.loads(self.rfile.read(length))
            principal = self.authenticate(payload)
            if self.path == '/commit':
                self.reply(self.server.confirm_commit(payload, principal))
                return
            if self.path == '/artifact/read':
                self.server.authorize(principal, 'artifact.read', run_id=payload['run_id'])
                self.reply(self.server.artifact_store.read(payload['run_id'], payload['artifact_id'], payload['sha256']))
                return
            if self.path == '/artifact/upload':
                from bioma.core.models import Artifact
                self.server.authorize(principal, 'artifact.write', run_id=payload['run_id'])
                artifact = Artifact.model_validate(payload['artifact'])
                if artifact.metadata.get('run_id') != payload['run_id'] or artifact.metadata.get('sha256') != payload['sha256']:
                    raise ValueError('Artifact upload binding mismatch')
                self.server.artifact_store.import_blob(payload['run_id'], artifact, payload['data'])
                self.reply({'accepted': artifact.id})
                return
            if self.path == '/replicate':
                if self.server.identity:
                    envelope = payload
                    origin, payload = self.server.trust.verify(envelope, 'bioma.node.event.v1')
                    info = payload['payload']
                    run_id = info['run_id']
                    if not run_id or origin != info.get('origin_node') or payload['kind'] not in {'Requirement', 'DeliveryResult', 'ExecutionFailure'}:
                        raise PermissionError('Invalid event origin or kind')
                    agent = info.get('delta', {}).get('agent_id') or info.get('recipient')
                    if not agent:
                        raise PermissionError('Event must identify its provider')
                    self.server.authorize(principal, 'node.replicate', run_id=run_id)
                    self.server.authorize(origin, 'node.publish', run_id=run_id, agent=agent)
                self.server.journal.receive(payload, peers=self.server.peers)
                if self.server.identity:
                    with self.server.journal.connect() as db:
                        db.execute('INSERT OR IGNORE INTO event_signatures VALUES(?,?)', (payload['id'], json.dumps(envelope)))
                self.reply({'accepted': payload['id']})
                return
            if self.path == '/submit':
                event = self.server.submit(payload, principal=principal)
                from threading import Thread
                Thread(target=self.server.tick, daemon=True).start()
                self.reply({'accepted': event['id']})
                return
            self.reply({'error': 'not found'}, 404)
        except PermissionError as exc:
            self.reply({'error': str(exc)}, 403)
        except Exception as exc:
            self.reply({'error': str(exc)}, 400)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--store', required=True, type=Path)
    parser.add_argument('--peer', default='scientific-worker')
    parser.add_argument('--port', type=int, default=8021)
    parser.add_argument('--replicate-to', action='append', default=[])
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--local-insecure', action='store_true', help='Explicit loopback-only development mode')
    parser.add_argument('--security-config', type=Path, help='Pinned public keys, grants and private-key environment variable name')
    parser.add_argument('--tls-cert', type=Path)
    parser.add_argument('--tls-key', type=Path)
    args = parser.parse_args()
    import os
    import ssl
    from bioma.runtime.security import Identity
    options = {'local_insecure': args.local_insecure}
    peers = args.replicate_to
    if args.security_config:
        config = json.loads(args.security_config.read_text(encoding='utf-8'))
        identity = Identity.from_private_hex(config['node_id'], os.environ[config['private_key_env']])
        trust = TrustStore.from_dict(config['trusted_principals'])
        clients = {}
        peers = []
        for node_id, peer in config.get('peers', {}).items():
            tls = ssl.create_default_context(cafile=peer.get('ca_file'))
            client = NodeClient(peer['endpoint'], identity=identity, trust=trust, server_id=node_id, ssl_context=tls)
            clients[node_id] = clients[peer['endpoint'].rstrip('/')] = client
            peers.append(peer['endpoint'].rstrip('/'))
        options.update(identity=identity, trust=trust, peer_clients=clients,
            observation_trust=TrustStore.from_dict(config.get('observation_principals', {})))
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error('Both --tls-cert and --tls-key are required')
    if args.tls_cert:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(args.tls_cert, args.tls_key)
        options['ssl_context'] = tls
    provider = sequence_backend(args.peer, base_dir=args.store)
    server = NodeServer((args.host, args.port), provider, args.store, peers,
        export_roots=[args.store/'artifacts'], **options)
    from threading import Event, Thread
    stop = Event()
    def sync():
        while not stop.wait(1):
            server.replicate()
            server.tick()
    Thread(target=sync, daemon=True).start()
    print(f'Node {args.peer} ready on {args.port}', flush=True)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()


if __name__ == '__main__':
    main()
