"""Bounded public scientific HTTP reads with durable, hash-bound local files."""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlsplit, unquote
from uuid import uuid4

from pydantic import Field
from bioma.tools.base import ToolInput, bioma_tool

# Only public scientific services; redirects are checked before each request.
_HOSTS = ('ncbi.nlm.nih.gov', 'ebi.ac.uk', 'uniprot.org', 'rcsb.org',
          'zenodo.org', 'figshare.com', 'github.com', 'raw.githubusercontent.com')


def allowed_url(url):
    value = urlsplit(url)
    host = (value.hostname or '').lower()
    # Figshare's public file endpoint redirects to this specific storage bucket.
    # Do not authorize arbitrary S3 buckets or normalize a traversal into one.
    path = unquote(value.path)
    figshare_storage = (host == 's3-eu-west-1.amazonaws.com'
                        and path.startswith('/pfigshare-u-files/')
                        and '..' not in path.split('/') and '\\' not in path)
    if (value.scheme != 'https' or value.username or value.password
            or value.port not in {None, 443}
            or not (figshare_storage or any(host == domain or host.endswith('.' + domain) for domain in _HOSTS))):
        raise ValueError('Unauthorized public resource host/path: ' + host + value.path +
                         '. Only HTTPS public scientific hosts and Figshare public storage are allowed; no credentials or custom ports.')
    return url


class PublicResourceInput(ToolInput):
    url: str = Field(description='Exact public HTTPS URL discovered from source metadata. Supports NCBI/GEO, EBI, UniProt, RCSB, Zenodo, Figshare and GitHub public files.')
    max_bytes: int = Field(default=20_000_000, ge=1, le=500_000_000)


def validate_resource(result, arguments):
    try:
        return (result['status'] == 'ok' and result['requested_url'] == arguments['url']
                and 0 < result['bytes'] <= arguments.get('max_bytes', 20_000_000)
                and len(result['files']) == 1 and all(
                    Path(p).stat().st_size == result['bytes'] and
                    hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest
                    for p, digest in result['files'].items()))
    except (OSError, KeyError, TypeError, ValueError):
        return False


@bioma_tool(name='read_public_resource', category='database', author='BioMA',
            description='Read or download an exact public scientific HTTPS resource (GEO/NCBI, UniProt, EBI, RCSB, public GitHub, Zenodo, Figshare). Returns a bounded text preview and a hash-bound file usable by subsequent Python/R code. Use source metadata to discover data links; never infer missing user inputs before checking an explicitly requested public dataset.',
            args_schema=PublicResourceInput, requires_network=True,
            result_kind='retrieved_fact', result_validator=validate_resource,
            replay_safe=True, tags=['public-data', 'GEO', 'sequence', 'example-data'])
def read_public_resource(url: str, max_bytes: int = 20_000_000):
    import requests
    from urllib.parse import urljoin, unquote
    from bioma.runtime.deadline import check, remaining
    current = allowed_url(url)
    for _ in range(6):
        check()
        left = remaining()
        response = requests.get(current, stream=True, allow_redirects=False,
                                timeout=min(60, left) if left is not None else 60)
        if response.is_redirect:
            next_url = urljoin(current, response.headers['Location'])
            response.close()
            current = allowed_url(next_url)
            continue
        break
    else:
        raise ValueError('Too many redirects')
    root = Path(__file__).resolve().parents[2] / 'artifacts' / 'public_resources' / uuid4().hex
    root.mkdir(parents=True)
    name = Path(unquote(urlsplit(current).path)).name or 'resource.txt'
    path = root / (name if name not in {'.', '..'} else 'resource.txt')
    size = 0
    digest = hashlib.sha256()
    preview = b''
    try:
        with response:
            response.raise_for_status()
            with path.open('wb') as stream:
                for chunk in response.iter_content(1024 * 1024):
                    check()
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError('Resource exceeds max_bytes; select a smaller resource or explicitly increase the bounded limit.')
                    stream.write(chunk)
                    digest.update(chunk)
                    if len(preview) < 16000:
                        preview += chunk[:16000-len(preview)]
        if size == 0:
            raise ValueError('The public endpoint returned an empty body. Follow its metadata API or a documented alternative download link; this is not an available dataset.')
        if b'RecaptchaChallengePage' in preview or b'<title>Access Denied' in preview:
            raise ValueError('Public service returned an access challenge, not scientific data. Try its documented API or public data repository; do not treat the page as input measurements.')
        content_type = response.headers.get('Content-Type', '')
        text = (preview.decode('utf-8', errors='replace') if
                any(kind in content_type for kind in ('text', 'json', 'xml')) else None)
        return {'status':'ok', 'requested_url':url, 'resolved_url':current,
                'bytes':size, 'content_type':content_type, 'preview':text,
                'preview_truncated':size > len(preview), 'files':{str(path):digest.hexdigest()},
                'qualification':'retrieved bytes only; not scientific evidence'}
    except BaseException:
        path.unlink(missing_ok=True)
        raise
