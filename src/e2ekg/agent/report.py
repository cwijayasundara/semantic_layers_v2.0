"""The agent's two typed outputs: a specialist's `FindingIn` (recorded in the graph as an
ops:Finding) and the orchestrator's `RCAReport`."""
from typing import Literal

from pydantic import BaseModel

from e2ekg.scenario.model import AppId


class FindingIn(BaseModel):
    """What a specialist concludes about one application. `signal_uris` are the graph nodes it
    rests on; a finding without any is rejected. SHACL, not this model, bounds `confidence`
    to [0, 1], so the rejection text names the shape that failed."""
    about_app: AppId
    claim: str
    signal_uris: list[str]
    confidence: float


class Hypothesis(BaseModel):
    root_cause_uri: str
    chain: list[str]
    confidence: float
    evidence: list[str]
    refuted_alternatives: list[str]


class RCAReport(BaseModel):
    verdict: Literal["root_cause_found", "inconclusive"]
    hypotheses: list[Hypothesis]
    summary: str
