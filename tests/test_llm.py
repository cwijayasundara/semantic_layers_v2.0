import sys
import types

import pytest
from pydantic import BaseModel

from e2ekg.llm import MODEL_ID, FakeClient, FireworksClient, LLMContractError, request_key


class Pick(BaseModel):
    choice: str


def test_model_id():
    assert MODEL_ID == "accounts/fireworks/models/glm-5p3-flash"


def test_fake_client_keyed_and_errors():
    k = request_key(MODEL_ID, "s", "u", Pick)
    assert FakeClient({k: Pick(choice="a")}).structured("s", "u", Pick).choice == "a"
    with pytest.raises(LLMContractError):
        FakeClient({}).structured("s", "u", Pick)
    with pytest.raises(LLMContractError):
        FakeClient({FakeClient.ANY_KEY: LLMContractError("x")}).structured("s", "u", Pick)


def _fake_chat(monkeypatch, outputs):
    calls = []

    class Chat:
        def __init__(self, model, timeout=None):
            self.model = model

        def with_structured_output(self, schema):
            class R:
                def invoke(_, msgs):
                    calls.append(msgs)
                    o = outputs.pop(0)
                    if isinstance(o, Exception):
                        raise o
                    return o
            return R()
    mod = types.ModuleType("langchain_fireworks")
    mod.ChatFireworks = Chat
    monkeypatch.setitem(sys.modules, "langchain_fireworks", mod)
    return calls


def test_llm_cache_hits(tmp_path, monkeypatch):
    calls = _fake_chat(monkeypatch, [Pick(choice="x")])
    c = FireworksClient(cache_dir=tmp_path)
    assert c.structured("s", "u", Pick).choice == "x"
    assert c.structured("s", "u", Pick).choice == "x"   # second call: disk cache
    assert FireworksClient(cache_dir=tmp_path).structured("s", "u", Pick).choice == "x"
    assert len(calls) == 1


def test_retries_then_contract_error(tmp_path, monkeypatch):
    calls = _fake_chat(monkeypatch, [ValueError("bad")] * 3)
    with pytest.raises(LLMContractError):
        FireworksClient(cache_dir=tmp_path).structured("s", "u", Pick)
    assert len(calls) == 3   # 1 try + 2 retries


def test_retry_recovers(tmp_path, monkeypatch):
    _fake_chat(monkeypatch, [ValueError("bad"), Pick(choice="ok")])
    assert FireworksClient(cache_dir=tmp_path).structured("s", "u", Pick).choice == "ok"
