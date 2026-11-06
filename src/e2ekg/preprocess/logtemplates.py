"""Log fingerprints: Drain3 templates, hourly buckets covering every row, and spikes.

Templates are mined on the distinct digit-masked messages in sorted order, so the
result is deterministic and cheap (the message vocabulary is small next to the line
count). `template_id` is the first 12 hex chars of sha1(template text); variable
tokens, whether Drain's or the masks', are written `<*>`.

Spikes: 1-minute buckets per (app, template); robust z of the count against the
median and MAD of the trailing 60 minutes (the current minute excluded; a series
needs at least 5 prior minutes). sigma = max(1.4826*MAD, sqrt(median), 1), a Poisson
floor, so a chronic template with a noisy but steady rate does not alarm. A spike
is a run of consecutive minutes with z >= 6 and count >= 20, extended while the
rate holds at half the burst level (a long burst is one spike, not its first minutes)."""
import hashlib
import re
import warnings
from datetime import timedelta

import numpy as np
import polars as pl
from drain3 import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig
from numpy.lib.stride_tricks import sliding_window_view

from e2ekg.preprocess import selector as sel

SIM_TH, DEPTH = 0.4, 4
Z_MIN, COUNT_MIN, BASE_MIN, BASE_WINDOW = 6.0, 20, 5, 60
_LEVELS = {"TRACE": 0, "DEBUG": 1, "INFO": 2, "NOTICE": 2, "WARN": 3, "WARNING": 3,
           "ERROR": 4, "SEVERE": 4, "CRITICAL": 5, "FATAL": 5}
_RANK_NAME = {0: "TRACE", 1: "DEBUG", 2: "INFO", 3: "WARN", 4: "ERROR", 5: "FATAL"}
_VAR = re.compile(r"<\*>|<NUM>|<HEX>")


def mask_expr(col: str = "msg") -> pl.Expr:
    return (pl.col(col).fill_null("").str.replace_all(r"\b[0-9a-f]{12,}\b", "<HEX>")
            .str.replace_all(r"\d+", "<NUM>"))


def template_id(template: str) -> str:
    return hashlib.sha1(template.encode()).hexdigest()[:12]


def _params(template: str, msg: str) -> list[str]:
    parts = _VAR.split(template)
    rx = "(" + r"\S+" + ")"
    pattern = "^" + rx.join(re.escape(p) for p in parts) + "$"
    m = re.match(pattern, " ".join(msg.split()))
    return list(m.groups()) if m else []


def mine_templates(logs_df: pl.DataFrame) -> pl.DataFrame:
    """Adds template_id, template and params (list of the variable tokens) per row."""
    masked = logs_df.select(mask_expr().alias("_m"))["_m"]
    uniq = sorted(set(masked.unique().to_list()))
    cfg = TemplateMinerConfig()
    cfg.drain_sim_th, cfg.drain_depth, cfg.profiling_enabled = SIM_TH, DEPTH, False
    tm = TemplateMiner(persistence_handler=None, config=cfg)
    cluster_of = {m: tm.add_log_message(m)["cluster_id"] for m in uniq}
    final = {cid: _VAR.sub("<*>", c.get_template()) for cid, c in tm.drain.id_to_cluster.items()}
    tmpl_of = {m: final[cid] for m, cid in cluster_of.items()}
    lut = pl.DataFrame({"_m": list(tmpl_of), "template": list(tmpl_of.values())})
    lut = lut.with_columns(template_id=pl.col("template").map_elements(
        template_id, return_dtype=pl.String))
    out = (logs_df.with_columns(_m=masked).join(lut, on="_m", how="left", maintain_order="left")
           .drop("_m"))
    # params once per distinct (template, msg), then joined back
    pairs = out.select("template", pl.col("msg").fill_null("").alias("_msg")).unique()
    pairs = pairs.with_columns(params=pl.struct("template", "_msg").map_elements(
        lambda r: _params(r["template"], r["_msg"]), return_dtype=pl.List(pl.String)))
    return (out.with_columns(_msg=pl.col("msg").fill_null(""))
            .join(pairs, on=["template", "_msg"], how="left", maintain_order="left").drop("_msg"))


def _rank(level: pl.Expr) -> pl.Expr:
    return level.str.to_uppercase().replace_strict(_LEVELS, default=-1, return_dtype=pl.Int64)


def log_buckets(logs_df: pl.DataFrame) -> pl.DataFrame:
    """One row per (app, template, hour); the counts sum to the number of log rows."""
    g = (logs_df.with_columns(hour_start=pl.col("ts_utc").dt.truncate("1h"),
                              _rank=_rank(pl.col("level").fill_null("")))
         .group_by("app_uri", "template_id", "hour_start", maintain_order=False)
         .agg(pl.len().alias("count"), pl.col("_rank").max().alias("_rank"))
         .sort("app_uri", "template_id", "hour_start", nulls_last=True))
    g = g.with_columns(error_level=pl.col("_rank").replace_strict(
        _RANK_NAME, default=None, return_dtype=pl.String)).drop("_rank")
    sels = [sel.make("logs", a, t, h, None if h is None else h + timedelta(hours=1))
            for a, t, h in g.select("app_uri", "template_id", "hour_start").iter_rows()]
    return g.with_columns(selector=pl.Series(sels, dtype=pl.String))


def log_spikes(logs_df: pl.DataFrame) -> pl.DataFrame:
    schema = {"app_uri": pl.String, "template_id": pl.String, "template": pl.String,
              "t_start": pl.Datetime("us", "UTC"), "t_end": pl.Datetime("us", "UTC"),
              "count": pl.Int64, "baseline_rate": pl.Float64, "z": pl.Float64,
              "selector": pl.String}
    timed = logs_df.filter(pl.col("ts_utc").is_not_null()).with_columns(
        minute=pl.col("ts_utc").dt.truncate("1m"))
    if timed.height == 0:
        return pl.DataFrame(schema=schema)
    c = timed.group_by("app_uri", "template_id", "minute").len()
    hot = c.filter(pl.col("len") >= COUNT_MIN).select("app_uri", "template_id").unique()
    c = c.join(hot, on=["app_uri", "template_id"], how="semi")
    t0 = timed["minute"].min()
    n = int((timed["minute"].max() - t0).total_seconds() // 60) + 1
    text = timed.group_by("template_id").agg(pl.col("template").first())
    names = dict(zip(text["template_id"], text["template"], strict=True))
    rows = []
    for (app, tid), grp in c.group_by(["app_uri", "template_id"], maintain_order=True):
        x = np.zeros(n)
        idx = ((grp["minute"] - t0).dt.total_seconds() // 60).cast(pl.Int64).to_numpy()
        x[idx] = grp["len"].to_numpy()
        w = sliding_window_view(np.concatenate([np.full(BASE_WINDOW, np.nan), x]),
                                BASE_WINDOW)[:n]
        valid = (~np.isnan(w)).sum(axis=1)
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            med = np.nanmedian(w, axis=1)
            mad = np.nanmedian(np.abs(w - med[:, None]), axis=1)
        sigma = np.maximum.reduce([1.4826 * mad, np.sqrt(med), np.ones(n)])
        z = (x - med) / sigma
        flag = (valid >= BASE_MIN) & (z >= Z_MIN) & (x >= COUNT_MIN)
        i = 0
        while i < n:
            if not flag[i]:
                i += 1
                continue
            j = i
            # a sustained burst stays one spike: the trailing baseline soon absorbs it, so
            # keep going while the rate holds at half the burst level (and above the
            # pre-spike median + 6 sigma), ending after 3 minutes below that
            level = max(0.5 * float(np.median(x[i:j + 1])), med[i] + Z_MIN * sigma[i])
            m, miss = j + 1, 0
            while m < n and miss < 3:
                if x[m] >= level:
                    j, miss = m, 0
                else:
                    miss += 1
                m += 1
            ts, te = t0 + timedelta(minutes=i), t0 + timedelta(minutes=j + 1)
            rows.append({"app_uri": app, "template_id": tid, "template": names[tid],
                         "t_start": ts, "t_end": te, "count": int(x[i:j + 1].sum()),
                         "baseline_rate": float(med[i]), "z": float(z[i:j + 1].max()),
                         "selector": sel.make("logs", app, tid, ts, te)})
            i = j + 1
    return pl.DataFrame(rows, schema=schema).sort("t_start", "app_uri", "template_id")

