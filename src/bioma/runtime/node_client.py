"""Authenticated requests bind method, path, audience, body and fresh nonce."""
import ipaddress
import json
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler, ProxyHandler
from uuid import uuid4

from bioma.runtime.security import canonical, digest


def loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == 'localhost'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise PermissionError('Node redirects are forbidden')


class NodeClient:
    def __init__(self, endpoint, *, identity=None, trust=None, server_id=None,
                 local_insecure=False, ssl_context=None):
        parsed = urlsplit(endpoint)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('Node endpoint must be an origin without credentials or path')
        if parsed.scheme not in ('http', 'https'):
            raise ValueError('Unsupported node transport')
        if not loopback(parsed.hostname or '') and parsed.scheme != 'https':
            raise ValueError('Remote nodes require HTTPS')
        if identity is None or trust is None or server_id is None:
            if not local_insecure or not loopback(parsed.hostname or ''):
                raise ValueError('Node identity and pinned server trust are required')
        elif local_insecure:
            raise ValueError('Secure configuration cannot downgrade to local_insecure')
        self.endpoint = endpoint.rstrip('/')
        self.identity, self.trust, self.server_id = identity, trust, server_id
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=ssl_context))

    def request(self, path, payload=None):
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('Invalid node path')
        method = 'GET' if payload is None else 'POST'
        body = b'' if payload is None else canonical(payload)
        headers = {'Content-Type': 'application/json'}
        nonce = uuid4().hex
        if self.identity:
            import base64
            envelope = self.identity.sign('bioma.node.request.v1', {
                'method': method, 'path': path, 'body_sha256': digest(payload),
                'audience': self.server_id, 'timestamp': time.time(), 'nonce': nonce})
            headers['X-BioMA-Auth'] = base64.b64encode(canonical(envelope)).decode()
        request = Request(self.endpoint + path, data=None if payload is None else body, headers=headers, method=method)
        with self.opener.open(request, timeout=90) as response:
            raw = response.read(48 * 1024 * 1024 + 1)
            if len(raw) > 48 * 1024 * 1024:
                raise ValueError('Node response exceeds limit')
            result = json.loads(raw)
        if self.identity:
            signer, signed = self.trust.verify(result, 'bioma.node.response.v1')
            if signer != self.server_id or signed.get('nonce') != nonce or signed.get('path') != path:
                raise PermissionError('Node response identity or request binding mismatch')
            result = signed['result']
        return result
