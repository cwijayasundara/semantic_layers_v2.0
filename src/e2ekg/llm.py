"""The one door to an LLM. Every call is a structured call against a closed
pydantic schema, cached on disk by the sha256 of the request.

`FireworksClient` is the real client (ChatFireworks + structured output, 2
retries). `FakeClient` replays canned responses keyed the same way, so tests
never touch the network."""
import hashlib
import json
import os
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError

MODEL_ID = "accounts/fireworks/models/glm-5p3-flash"
RETRIES = 2


class LLMContractError(Exception):
    """The model's answer did not satisfy the closed schema (after retries)."""


class LLMTransportError(Exception):
    """The call itself failed (network, rate limit, server error) after the retries."""


class LLMClient(Protocol):
    def structured(self, system: str, user: str, schema: type[BaseModel]) -> BaseModel: ...


def request_key(model: str, system: str, user: str, schema: type[BaseModel]) -> str:
    blob = json.dumps([model, system, user, schema.model_json_schema()], sort_keys=True,
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


class FireworksClient:
    def __init__(self, model: str = MODEL_ID, cache_dir: Path = Path(".cache/llm")) -> None:
        self.model, self.cache_dir = model, Path(cache_dir)
        self._chat = None

    def _structured_chat(self, schema: type[BaseModel]):
        # imported lazily: tests that never call the model need no key or network
        from langchain_fireworks import ChatFireworks
        return ChatFireworks(model=self.model).with_structured_output(schema)

    def structured(self, system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        key = request_key(self.model, system, user, schema)
        path = self.cache_dir / f"{key}.json"
        if path.exists():
            try:
                return schema.model_validate_json(path.read_text())
            except (ValidationError, ValueError, OSError):
                pass  # a torn or stale cache entry is a miss
        runnable = self._structured_chat(schema)
        last: Exception | None = None
        contract = False
        for _ in range(1 + RETRIES):
            try:
                out = runnable.invoke([("system", system), ("human", user)])
                result = out if isinstance(out, schema) else schema.model_validate(out)
            except (ValidationError, ValueError, TypeError) as e:
                last, contract = e, True
                continue
            except Exception as e:  # transport, rate limit, server error: retry, then report
                last, contract = e, False
                continue
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(result.model_dump_json())
            tmp.replace(path)           # atomic: readers never see a half-written entry
            return result
        if contract:
            raise LLMContractError(f"{schema.__name__}: {last}") from last
        raise LLMTransportError(f"{schema.__name__}: {type(last).__name__}: {last}") from last


class FakeClient:
    """`responses` maps request_key -> a model (returned) or an Exception (raised).
    The key `ANY_KEY` is the fallback for requests with no exact entry; with no
    fallback an unknown request raises LLMContractError."""

    ANY_KEY = "*"

    def __init__(self, responses: dict[str, BaseModel | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, str]] = []

    def structured(self, system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        key = request_key(MODEL_ID, system, user, schema)
        self.calls.append((key, system, user))
        resp = self.responses.get(key, self.responses.get(self.ANY_KEY))
        if resp is None:
            raise LLMContractError(f"FakeClient: no response for {key[:12]}")
        if isinstance(resp, Exception):
            raise resp
        return resp


def __getattr__(name: str):
    # ScriptedClient imports the pipeline schemas, which import this module: load lazily.
    if name == "ScriptedClient":
        from e2ekg.scripted_llm import ScriptedClient
        return ScriptedClient
    raise AttributeError(name)
