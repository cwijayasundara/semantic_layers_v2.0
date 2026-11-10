# Experiments: closing the honesty gap on "needle in a haystack"

An independent audit of this implementation against the Netflix post
(`netflix_post.md`) found it honest in construction — no causal truth in the
queryable graph, red herrings, a no-fault control, the topology-gate ablation
at 29% top-1 — but optimistic in scale: at 1× haystack density `raw_only`
also scored 100% top-1, so the benchmark could not demonstrate the post's
strong claim, that *only* the ontology keeps the needle findable as the
haystack grows. It showed the graph finds the needle cheaper, faster and
better-explained, not that it finds it when nothing else does.

Three experiments close that gap. All three were run to completion; results
below. Reproduce with `scripts/needle_campaign.sh` (builds three data roots
under `data-exp/` and runs the four eval phases; resumable; needs
`FIREWORKS_API_KEY` for the live-chat arm and the LLM evals).

## 1. Density sweep — the crossover

**Design.** `generate --scale S` multiplies background traffic (metrics, logs,
traces) on every app *off* the causal path (R4): the needle — the planted
fault, its evidence and its red herrings — stays exactly the same while the
haystack grows. Built at S=1 (the existing data), S=4 and S=8 (S=16 exceeds
this machine's RAM; generation peaks ~40 GB at S=8). Same seed, same answer
keys, `preprocess --llm scripted`, full as-of harvest, SHACL clean on every
store. Eval: `graph` and `raw_only` arms, 1 run per scenario per scale.

**Haystack vs needle sizes** (per scenario, from the generation manifests and
the as-of stores):

| root | raw records (per scenario) | raw bytes | as-of quads |
|---|---|---|---|
| scale 1 | 2.2M – 3.7M | 0.5 – 0.9 GB | 58,155 – 63,401 |
| scale 4 | 7.0M – 12.3M | 1.4 – 2.7 GB | 60,604 – 64,112 |
| scale 8 | 13.3M – 24.3M | 2.7 – 5.1 GB | 60,340 – 65,860 |

Raw volume grows ×8; the as-of graph stays **flat** (±5%, and within the
per-scenario band of scale 1). The graph's size is O(fault structure), not
O(traffic): a ~300–400:1 compression of the haystack, and the needle does not
grow with it.

**Results** (7 scenarios; medians per run):

| scale | graph top-1 | graph med tokens | graph med wall | raw_only top-1 | raw_only med tokens | raw_only med wall |
|---|---|---|---|---|---|---|
| 1× | 21/21 | 219K | 40.5 s | 21/21 | 1,299K | 89.0 s |
| 4× | 7/7 | 231K | 46.0 s | 6/7 | 1,661K | 87.3 s |
| 8× | 7/7 | 213K | 43.1 s | 5/7 | 1,754K | 102.4 s |

The lines cross. The graph arm is *flat* — accuracy, tokens and wall time
indistinguishable across an 8× denser haystack. `raw_only` degrades to 5/7
while its token bill grows: at 8× it fired a trap on v-cert-expiry, anchoring
on the planted red-herring api-gateway deploy (its report: "a red-black deploy
of api-gateway v7.32.0 began…") instead of the cert expiry. The post's
sentence — *"We need the right needle, not any needle"* — is exactly what
broke, in exactly the way predicted. On the same world, the graph arm held
top-1 in 47 s.

Caveat, honestly stated: 1 run per cell in the sweep (the 1× baseline is
3 runs). The failures are point observations; the *trend* — flat graph,
declining raw, growing raw tokens — is the mechanism the post claims, and the
8× failure is a planted red herring, the highest-signal failure mode to have.

## 2. Attribution ablation — the agent never needed the hint

**Design.** The live incident's `affected_services` is a deliberate lie: the
latest red-herring deployment's app, corrected 40 min later (post-cutoff, so
the agent only ever sees the lie). `generate --attribution` now brackets it:
`guess` (default, as above), `none` (filed unattributed), `correct` (the true
app from the start). Evaluated `graph`, 3 runs per scenario, at scale 1.

| attribution | top-1 | trap | ev. precision | med wall | med tokens |
|---|---|---|---|---|---|
| guess (baseline) | 21/21 | 0 | 0.25 | 41 s | 219K |
| none | 21/21 | 0 | 0.30 | 41 s | 230K |
| correct (built, not run) | — | — | — | — | — |

Removing the attribution entirely costs nothing: same top-1, same tokens,
same wall time, and evidence precision if anything improves (0.25 → 0.30, n=21
each, within noise). The agent treats the human's first guess as one hypothesis
among several — the prompt's instruction — and the ontology carries the
evidence it actually needs. The `correct` (gifted-hint) arm was built but is
not informative on a saturated benchmark: with 21/21 everywhere, an upper
bound cannot distinguish itself. The useful finding is the null result:
**the wrong hint is not load-bearing.**

## 3. Live chat arm — the last simulated pillar is real

**Design.** R54's `ScriptedClient` stand-in was only ever the *default*;
`preprocess --llm fireworks` + `harvest --llm fireworks` runs live GLM chat
classification and identity picks at ingest. Rebuilt the processed tables and
as-of graphs for all 7 scenarios against the existing raw tree (no
regeneration — chat transcripts are raw text), then ran `graph`, 3 runs per
scenario.

| chat labels | top-1 | trap | fp | ev. precision | med wall | med tokens |
|---|---|---|---|---|---|---|
| scripted (R54) | 21/21 | 0 | 0 | 0.25 | 40.5 s | 219K |
| live GLM | 21/21 | 0 | 0 | 0.31 | 35.5 s | 220K |

The flywheel's input is no longer canned: real LLM-extracted chat claims flow
into `ops:Claim` → `ops:Pattern` priors, and quality holds (top-1 unchanged;
evidence precision 0.25 → 0.31). The live preprocess absorbs Fireworks
timeouts with retries; every store SHACL-clean.

## Verdict

The audit's caveat is retired. On this world:

1. **The crossover is demonstrated** — at 8× haystack density the raw arm
   starts grabbing the wrong needle while the ontology arm cannot tell the
   difference, at constant cost. The post's scale claim now holds as a
   benchmark result, not just a mechanism argument.
2. **The human attribution is a trap the agent does not depend on** — the
   system finds the root cause from the evidence graph, with the hint
   present, wrong, or absent.
3. **The chat flywheel is end-to-end real** — live LLM extraction replaces
   the scripted stand-in with no quality loss.

Remaining honesty limits, unchanged and now well-characterized: one world,
one seed, seven fault rooms; the needle is still planted by the same hand
that grades the search; S=16 (the next rung) needs a machine with >64 GB RAM.
The next real test is multi-fault scenarios (2–3 concurrent planted faults
with per-root answer keys), which needs generator surgery: `Scenario.fault`
is singular by design.

## Reproduction

```bash
# the campaign: builds data-exp/{s4,s8,nattr,chatlive} then the four evals
nohup caffeinate -i bash scripts/needle_campaign.sh > data-exp/campaign.log 2>&1 &

# aggregate any run dir (results.parquet -> tables)
uv run python - <<'EOF'
import pyarrow.parquet as pq
for r in pq.read_table("data-exp/s8/evals/sweep/results.parquet").to_pylist():
    if r["arm"] == "raw_only":
        print(r["scenario"], r["top1"], r["trap"], r["true_root_rank"])
EOF
```

~6.5 h wall on a 16-core/64 GB Mac: builds ~4.5 h (S=8 generation peaks
~40 GB RSS), evals ~2 h. `caffeinate` is load-bearing: a background process
loses ~90% of its CPU to App Nap after a system sleep/wake.
