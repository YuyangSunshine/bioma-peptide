"""DeepResearch: read-only external knowledge acquisition with source provenance."""
from collections.abc import Sequence
from typing import Protocol

from bioma.backends.base import MethodBinding, ScientificBackend
from bioma.backends.catalog import method
from bioma.core.scientific_capabilities import RESEARCH, ResearchBundle, Source, WorkResult


class ResearchSourceClient(Protocol):
    def search(self, query: str) -> Sequence[Source]: ...
    def fetch(self, source: Source) -> Source: ...
    def expand_citations(self, source: Source) -> Sequence[Source]: ...
    def synthesize(self, source_set: Sequence[Source], question: str) -> ResearchBundle: ...


def build_research_backend( backend: ResearchSourceClient | None = None, *, tools=(), additional_backends=(),
             scoped_skill_resolver=None):
    if any((t.external_effect and not t.replay_safe) or t.result_kind not in {'retrieved_fact', 'source', 'source_set'} for t in tools):
        raise ValueError('Research accepts read-only retrieval tools only')
    bindings = [MethodBinding(method(RESEARCH, name, 'ResearchBundle', available=backend is not None,
        reason='No research backend configured'), research_executor(backend, name), tuple(tools)) for name in ('targeted_retrieval', 'literature_review', 'deep_research', 'database_lookup', 'citation_expansion')]
    return ScientificBackend(RESEARCH, 'scientific-research', [*bindings, *additional_backends],
                     scoped_skill_resolver=scoped_skill_resolver)



def research_executor(backend, method_id):
    def execute(state, message, tools):
        question = message.payload.get('objective') or state.objective
        inputs = message.payload.get('inputs', {})
        search_query = str(inputs.get('search_query') or question).strip()
        if not search_query:
            raise ValueError('research search query cannot be empty')
        limit = int(inputs.get('max_sources', 20))
        if not 1 <= limit <= 100:
            raise ValueError('max_sources must be between 1 and 100')
        seeds = [Source.model_validate(s) for s in inputs.get('sources', [])]
        # Reserve room for references; filling the limit with seed results
        # previously discarded every expanded source unconditionally.
        seed_limit = max(1, limit // 2) if method_id in {'citation_expansion', 'deep_research'} else limit
        sources = seeds or list(backend.search(search_query))[:seed_limit]
        sources = [Source.model_validate(backend.fetch(source)) for source in sources[:seed_limit]]
        if method_id in {'citation_expansion', 'deep_research'}:
            expanded = []
            for source in sources:
                expanded.extend(backend.expand_citations(source))
                if len(sources) + len(expanded) >= limit:
                    break
            sources = [*sources, *expanded][:limit]
        sources = list({source.id: source for source in sources}.values())
        known = {source.id: source.model_copy(deep=True) for source in sources}
        bundle = ResearchBundle.model_validate(backend.synthesize(
            [source.model_copy(deep=True) for source in sources], question))
        if any(known.get(source.id) != source for source in bundle.sources):
            raise ValueError('Synthesis introduced a source not acquired by this backend')
        bundle = bundle.model_copy(update={'retrieval_trace': [*bundle.retrieval_trace,
            {'method': method_id, 'query': search_query, 'scientific_question': question,
             'source_ids': list(known), 'max_sources': limit}]})
        return WorkResult(family=RESEARCH, kind='ResearchBundle', payload=bundle.model_dump(mode='json'),
            provenance={'backend': type(backend).__name__, 'method': method_id})
    return execute
