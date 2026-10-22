from types import SimpleNamespace

import pytest

from bioma.config import LLMConfig, load_config
from bioma.llm import OpenAICompatibleProvider


@pytest.mark.parametrize('temperature',[None,0.0,1.0])
@pytest.mark.parametrize('with_tools',[False,True])
@pytest.mark.parametrize('thinking_mode',[None,'enabled','disabled'])
def test_sampling_default_is_omitted_and_explicit_overrides_are_preserved(temperature,with_tools,thinking_mode):
    captured=[]
    response=SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
        message=SimpleNamespace(content='ok',tool_calls=[]))],usage=None)
    class Client:
        def __init__(self):
            self.chat=SimpleNamespace(completions=self)
        def with_options(self,**kwargs): return self
        def create(self,**kwargs):
            captured.append(kwargs)
            return response
    provider=object.__new__(OpenAICompatibleProvider)
    provider.config=LLMConfig(model='test',temperature=temperature,thinking_mode=thinking_mode)
    provider.client=Client()
    if with_tools:
        provider.chat_with_tools([{'role':'user','content':'hi'}],[{'type':'function'}])
    else:
        provider.chat([{'role':'user','content':'hi'}])
    if temperature is None:
        assert 'temperature' not in captured[0]
    else:
        assert captured[0]['temperature']==temperature
    if thinking_mode is None:
        assert 'extra_body' not in captured[0]
    else:
        assert captured[0]['extra_body'] == {'thinking': {'type': thinking_mode}}


def test_truncation_cannot_execute_partial_tools_or_retry_same_gateway_budget():
    from bioma.llm import ModelOutputBudgetError
    from bioma.gateway import LLMQueryInterpreter, QueryInterpretationError
    from bioma.core.harness import runtime_recovery_action
    calls = []
    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
        def with_options(self, **kwargs): return self
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='length',
                message=SimpleNamespace(content='{"partial":', tool_calls=[]))], usage=None)
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(model='test')
    provider.client = Client()
    with pytest.raises(ModelOutputBudgetError) as failure:
        provider.chat_with_tools([], [{'type': 'function'}])
    assert runtime_recovery_action(failure.value, 0) == 'blocked'
    with pytest.raises(ModelOutputBudgetError, match='budget exhausted'):
        LLMQueryInterpreter(provider, model_id='test', max_attempts=2).interpret(
            'Calculate', inputs={}, capabilities=(), session_context=[])
    assert len(calls) == 2


def test_shipped_config_uses_provider_sampling_defaults():
    c=load_config()
    configs=[c.gateway.llm,c.comagent.llm,c.proagent.llm,c.operagent.llm,
             c.predagent.step_fusion.coordinator.llm,
             *(m.llm for m in c.predagent.step_fusion.reference_models)]
    assert all(c.temperature is None for c in configs)


@pytest.mark.parametrize('parameter', ['max_tokens', 'max_completion_tokens'])
def test_explicit_output_budget_parameter_is_used(parameter):
    captured = []
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
        message=SimpleNamespace(content='ok', tool_calls=[]))], usage=None)
    class Client:
        def __init__(self): self.chat = SimpleNamespace(completions=self)
        def with_options(self, **kwargs): return self
        def create(self, **kwargs): captured.append(kwargs); return response
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(model='test', max_output_parameter=parameter)
    provider.client = Client()
    provider.chat([{'role':'user','content':'hi'}])
    assert captured[0][parameter] == provider.config.max_output_tokens
    assert ({'max_tokens', 'max_completion_tokens'} - {parameter}).isdisjoint(captured[0])


@pytest.mark.parametrize('error', [PermissionError('denied'), TimeoutError('deadline'),
    ConnectionError('transport'), RuntimeError('execution failure')])
def test_gateway_does_not_retry_or_reclassify_provider_failures(error):
    from bioma.gateway import LLMQueryInterpreter
    calls = []
    class Provider:
        def chat(self, *args, **kwargs):
            calls.append(1)
            raise error
    with pytest.raises(type(error)) as caught:
        LLMQueryInterpreter(Provider(), model_id='fixture', max_attempts=2).interpret(
            'Calculate', inputs={}, capabilities=(), session_context=[])
    assert caught.value is error
    assert len(calls) == 1
