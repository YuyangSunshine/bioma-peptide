"""Permission filtering precedes lexical/vector ranking. Embedding backends are explicit."""
import math
import re
from bioma.core.research import fingerprint


def tokens(text):
    return set(re.findall(r'[a-z0-9_]+|[\u4e00-\u9fff]', text.casefold()))


def valid_vector(vector, dimension):
    if (len(vector) != dimension or any(type(x) not in (int,float) or not math.isfinite(x) for x in vector)
            or not any(vector)):
        raise ValueError('Embedding dimension or numeric content is invalid')
    return [float(x) for x in vector]


class MemoryRetriever:
    def __init__(self, store, *, embedding=None, spec=None):
        if (embedding is None) != (spec is None):
            raise ValueError('Embedding callable and versioned spec must be configured together')
        self.store, self.embedding, self.spec = store, embedding, spec

    def search(self, owner, project, task_id, query, *, limit=8, max_chars=6000, visible_evidence=(), access_grants=(), purpose=""):
        candidates = self.store.candidates(owner,project,task_id,visible_evidence=visible_evidence, access_grants=access_grants, purpose=purpose)
        query_tokens = tokens(query)
        query_vector = valid_vector(self.embedding(query), self.spec.dimension) if self.embedding else None
        key = fingerprint(self.spec.model_dump(mode='json')) if self.spec else None
        rows = []
        for record in candidates:
            # File identity is checked again at recall, never inferred from a summary.
            from bioma.evidence.packages import file_digest
            try:
                if any(file_digest(a['uri']) != a['sha256'] for a in record.artifact_manifest):
                    continue
            except (OSError, ValueError, KeyError):
                continue
            words = tokens(record.text)
            lexical = len(query_tokens & words) / max(1,len(query_tokens | words))
            score = lexical
            if self.embedding:
                content_key = fingerprint([key,record.text])
                vector = self.store.vector(record.id,content_key)
                if vector is None:
                    vector = valid_vector(self.embedding(record.text),self.spec.dimension)
                    self.store.vector(record.id,content_key,vector)
                vector = valid_vector(vector,self.spec.dimension)
                cosine = sum(a*b for a,b in zip(query_vector,vector)) / math.sqrt(sum(a*a for a in query_vector)*sum(b*b for b in vector))
                score = .4*lexical + .6*cosine
            if score > 0:
                rows.append((score,record))
        rows.sort(key=lambda pair:(-pair[0],-pair[1].created_at,pair[1].id))
        selected, used = [], 0
        for score, record in rows[:max(0,min(limit,30))]:
            text = record.text[:max(0,max_chars-used)]
            if not text:
                break
            selected.append({'id':record.id,'owner':record.owner,'shared':record.owner != owner,
                'layer':record.layer,'status':record.status,'text':text,
                'evidence_refs':record.evidence_refs,'source_event':record.source_event,
                'score':score,'authority':'context_only',
                'source_event_hash':record.metadata.get('source_event_hash'),
                'qualification_model':record.metadata.get('qualification_model'),
                'scientific_support':record.metadata.get('scientific_support','NOT_VERIFIABLE')})
            used += len(text)
        return {'mode':'hybrid' if self.embedding else 'lexical', 'memories':selected}
