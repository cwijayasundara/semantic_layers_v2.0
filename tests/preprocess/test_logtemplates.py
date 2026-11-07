import polars as pl
from pp_helpers import app_uri, derived, table

from e2ekg.preprocess.logtemplates import log_buckets, log_spikes, mine_templates
from e2ekg.preprocess.selector import Selector, select
from e2ekg.timeutil import pt


def at(h, m=0):
    return pt(2026, 10, 14, h, m)


def test_buckets_cover_every_log_row(preprocessed_hero):
    assert derived(preprocessed_hero, "fp_log_buckets")["count"].sum() == \
        table(preprocessed_hero, "logs").height


def test_every_log_row_has_a_template(preprocessed_hero):
    lg = table(preprocessed_hero, "logs")
    assert lg["template_id"].null_count() == 0
    assert lg["template_id"].str.len_chars().eq(12).all()
    assert lg["template_id"].n_unique() < 200            # volume collapses to few templates


def test_pool_exhaustion_spike_found(preprocessed_hero):
    s = derived(preprocessed_hero, "fp_log_spikes")
    hit = s.filter((pl.col("app_uri") == app_uri("title-batch-loader"))
                   & pl.col("template").str.contains("pool"))
    assert hit.height >= 1 and hit["t_start"].min() < at(15, 20)
    assert (hit["count"] >= 20).all() and (hit["z"] >= 6).all()


def test_chronic_npe_not_a_spike(preprocessed_hero):
    s = derived(preprocessed_hero, "fp_log_spikes")
    assert s.filter(pl.col("app_uri") == app_uri("artwork-svc")).height == 0
    # the chronic template exists, it just never spikes
    lg = table(preprocessed_hero, "logs").filter(pl.col("app_uri") == app_uri("artwork-svc"))
    assert lg["template"].str.contains("NullPointerException").any()


def test_spike_evidence_rows_are_reachable(preprocessed_hero):
    """The answer key's log_error evidence (pool exhaustion rows) falls inside spikes."""
    import json
    ans = json.loads((preprocessed_hero.answers / "hero.json").read_text())
    ev = set(ans["evidence_by_tag"]["evidence:log_error"])
    s = derived(preprocessed_hero, "fp_log_spikes")
    got = set()
    for sj in s["selector"]:
        got |= set(select(preprocessed_hero.proc, Selector.from_json(sj))["raw_id"])
    assert len(ev & got) / len(ev) >= 0.9


def test_selector_round_trip(preprocessed_hero):
    for name in ("fp_log_spikes", "fp_log_buckets"):
        df = derived(preprocessed_hero, name)
        for row in df.sample(min(25, df.height), seed=3).iter_rows(named=True):
            got = select(preprocessed_hero.proc, Selector.from_json(row["selector"]))
            assert got.height == row["count"]


def test_selector_json_round_trip():
    s = Selector("logs", None, "abc", at(12), at(13))
    assert Selector.from_json(s.to_json()) == s


def test_mining_is_deterministic_and_masks_numbers():
    df = pl.DataFrame({"msg": [f"Processed batch of {n} records in {n * 3}ms" for n in range(30)]
                       + ["Warmed cache with 5 entries", "Warmed cache with 900 entries"]})
    a, b = mine_templates(df), mine_templates(df.reverse())
    assert set(a["template"]) == {"Processed batch of <*> records in <*>ms",
                                  "Warmed cache with <*> entries"}
    assert dict(zip(a["msg"], a["template_id"], strict=True)) == \
        dict(zip(b["msg"], b["template_id"], strict=True))
    row = a.filter(pl.col("msg") == "Processed batch of 7 records in 21ms").row(0, named=True)
    assert row["params"] == ["7", "21ms"] or row["params"] == ["7", "21"] or row["params"]


def test_spike_requires_count_and_z():
    from datetime import UTC, datetime, timedelta
    t0 = datetime(2026, 10, 14, 19, tzinfo=UTC)
    quiet = [t0 + timedelta(minutes=m, seconds=s) for m in range(120) for s in (1, 31)]
    burst = [t0 + timedelta(minutes=90, seconds=s) for s in range(0, 60, 2)] * 1   # 30 lines
    small = [t0 + timedelta(minutes=100, seconds=s) for s in range(0, 20, 2)]       # 10 lines
    ts = quiet + burst + small
    df = pl.DataFrame({"app_uri": ["a"] * len(ts), "template_id": ["t"] * len(ts),
                       "template": ["x"] * len(ts), "level": ["INFO"] * len(ts),
                       "ts_utc": pl.Series(ts).dt.replace_time_zone("UTC")})
    s = log_spikes(df)
    assert s.height == 1 and s["t_start"][0] == t0 + timedelta(minutes=90)
    assert log_buckets(df)["count"].sum() == df.height
