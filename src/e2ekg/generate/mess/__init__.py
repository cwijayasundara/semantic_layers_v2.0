"""The messiness registry (spec section 3) and `apply_mess` (Ruling R12).

apply_mess runs the structured injectors in registry order on `fields`,
serializes every record, then runs the bytes injectors in registry order on
`payload`. Every corruption is logged as a MessInjection. Injectors keep a
record's `tags`, copies carry them too, and nothing is deleted except the
bytes log.truncate cuts (kept in its detail) and the spans trc.sampling never
collected. met.gap nulls values in place and keeps them in its detail."""
from collections.abc import Sequence

import numpy as np

from e2ekg.generate.mess import chat, events, identity, logs, metrics, traces
from e2ekg.generate.mess._base import FnInjector, Injector, MessInjection, Phase
from e2ekg.generate.records import RawRecord, serialize
from e2ekg.scenario.model import Scenario

INJECTORS: tuple[FnInjector, ...] = (identity.INJECTORS + metrics.INJECTORS + logs.INJECTORS
                                     + traces.INJECTORS + events.INJECTORS + chat.INJECTORS)

__all__ = ["INJECTORS", "Injector", "MessInjection", "Phase", "apply_mess"]


def apply_mess(recs: Sequence[RawRecord], rng: np.random.Generator, scenario: Scenario,
               injectors: Sequence[Injector] = INJECTORS,
               ) -> tuple[list[RawRecord], list[MessInjection]]:
    out, log = list(recs), []
    for inj in (i for i in injectors if i.phase == "structured"):
        out, got = inj.apply(out, rng, scenario)
        log += got
    out = [serialize(r) for r in out]
    for inj in (i for i in injectors if i.phase == "bytes"):
        out, got = inj.apply(out, rng, scenario)
        log += got
    return out, log
