"""Run-scoped immutable blobs. Wire descriptors never contain host paths."""
import base64
import hashlib
import os
from pathlib import Path
import re
import sqlite3
from uuid import uuid4


class ArtifactStore:
    def __init__(self, root, *, allowed_roots=(), max_bytes=32 * 1024 * 1024):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.allowed_roots = tuple(Path(p).resolve() for p in allowed_roots)
        self.max_bytes = max_bytes
        self.db = self.root / 'index.sqlite3'
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE IF NOT EXISTS bindings(run TEXT, artifact TEXT, sha TEXT, PRIMARY KEY(run,artifact))')

    def _path(self, sha):
        if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
            raise ValueError('Invalid content address')
        return self.root / sha

    def _save(self, data, expected):
        if len(data) > self.max_bytes or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('Artifact size or digest mismatch')
        path = self._path(expected)
        temp = self.root / (uuid4().hex + '.tmp')
        try:
            temp.write_bytes(data)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return path

    def publish(self, run_id, artifact):
        path = Path(artifact.uri).resolve()
        if not any(path.is_relative_to(root) for root in self.allowed_roots):
            raise PermissionError('Artifact is outside configured export roots')
        with path.open('rb') as stream:
            data = stream.read(self.max_bytes + 1)
        sha = hashlib.sha256(data).hexdigest()
        if artifact.metadata.get('sha256', sha) != sha:
            raise ValueError('Artifact changed before export')
        self._save(data, sha)
        with sqlite3.connect(self.db) as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT sha FROM bindings WHERE run=? AND artifact=?', (run_id, artifact.id)).fetchone()
            if old and old[0] != sha:
                raise ValueError('Artifact identity cannot be rebound')
            db.execute('INSERT OR IGNORE INTO bindings VALUES(?,?,?)', (run_id, artifact.id, sha))
        return artifact.model_copy(update={'uri': 'bioma+sha256:' + sha,
            'metadata': {**artifact.metadata, 'sha256': sha, 'size_bytes': len(data), 'run_id': run_id,
                         'file_suffix': artifact.metadata.get('file_suffix', path.suffix)}})

    def read(self, run_id, artifact_id, sha):
        with sqlite3.connect(self.db) as db:
            row = db.execute('SELECT sha FROM bindings WHERE run=? AND artifact=?', (run_id, artifact_id)).fetchone()
        if row is None or row[0] != sha:
            raise PermissionError('Artifact is not published for this run')
        data = self._path(sha).read_bytes()
        if len(data) > self.max_bytes or hashlib.sha256(data).hexdigest() != sha:
            raise ValueError('Stored artifact integrity failure')
        return {'sha256': sha, 'data': base64.b64encode(data).decode()}

    def import_blob(self, run_id, artifact, data):
        sha = artifact.metadata.get('sha256')
        if artifact.uri != 'bioma+sha256:' + str(sha):
            raise ValueError('Expected a content address')
        path = self._save(base64.b64decode(data, validate=True), sha)
        local = artifact.model_copy(update={'uri': str(path)})
        # This store owns its cache directory; caller uploads never name a path.
        with sqlite3.connect(self.db) as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT sha FROM bindings WHERE run=? AND artifact=?', (run_id, artifact.id)).fetchone()
            if old and old[0] != sha:
                raise ValueError('Artifact identity cannot be rebound')
            db.execute('INSERT OR IGNORE INTO bindings VALUES(?,?,?)', (run_id, artifact.id, sha))
        return local

    def materialize(self, artifact, read):
        sha = artifact.metadata.get('sha256')
        if artifact.uri != 'bioma+sha256:' + str(sha):
            raise ValueError('Remote artifacts must use content addresses')
        response = read(artifact)
        if response.get('sha256') != sha:
            raise ValueError('Unexpected artifact response')
        data = base64.b64decode(response['data'], validate=True)
        path = self._save(data, sha)
        suffix = artifact.metadata.get('file_suffix', '')
        if suffix:
            if not isinstance(suffix, str) or not re.fullmatch(r'\.[A-Za-z0-9]{1,12}', suffix):
                raise ValueError('Invalid artifact suffix')
            path = self.root / (sha + suffix)
            temp = self.root / (uuid4().hex + '.tmp')
            try:
                temp.write_bytes(data)
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)
        return artifact.model_copy(update={'uri': str(path)})
