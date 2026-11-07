"""Metric enricher: one ops:MetricSeries per cleaned series, one ops:Anomaly per
detected change point (Task 8 fingerprints).

  * MetricSeries (`derived/fp_metric_series`): met:metricName (the series key
    before "|"), ops:onApplication (when resolved), ops:count, ops:windowStart /
    ops:windowEnd and ops:rawSelector (plus the no-time sentinel selector when
    the series has untimed rows). The selectors of all series together reach
    every metric row (HELP/TYPE lines included, as `<name>|meta`).
  * Anomaly (`derived/fp_metric_anomalies`): ops:onSeries, ops:onApplication,
    ops:atTime (onset) = ops:windowStart, ops:windowEnd, ops:magnitude,
    met:direction (extension) and the anomaly's own ops:rawSelector. An anomaly
    whose series key has no coverage row still gets a series node, carrying the
    anomaly's selector (it then has no ops:count)."""
from rdflib import URIRef
from rdflib.namespace import RDF

from e2ekg.graph.enricher import (
    NS, Enricher, dec, finite, integer, node, read_table, string, ts,
)

OPS, MET = NS["ops"], NS["met"]


class MetricAnomalyEnricher(Enricher):
    name = "metrics"
    reads = ("derived/fp_metric_series", "derived/fp_metric_anomalies")

    def fetch(self, ctx):
        series = read_table(ctx, "derived/fp_metric_series").sort("app_uri", "key", "t_start",
                                                                  nulls_last=True)
        anomalies = read_table(ctx, "derived/fp_metric_anomalies").sort(
            "app_uri", "series_key", "t_start", nulls_last=True)
        return series.to_dicts(), anomalies.to_dicts()

    def analyze(self, raw):
        rows, anomalies = raw
        series: dict[tuple, dict] = {}
        for r in rows:          # a series with untimed rows has a second, sentinel row
            d = series.setdefault((r["app_uri"], r["key"]), {
                "app_uri": r["app_uri"], "key": r["key"], "n": 0, "t_start": None,
                "t_end": None, "selectors": []})
            d["n"] += r["n"]
            d["selectors"].append(r["selector"])
            if r["t_start"] is not None:
                d["t_start"], d["t_end"] = r["t_start"], r["t_end"]
        for a in anomalies:
            series.setdefault((a["app_uri"], a["series_key"]), {
                "app_uri": a["app_uri"], "key": a["series_key"], "n": None, "t_start": None,
                "t_end": None, "selectors": [a["selector"]]})
        return list(series.values()), anomalies

    def emit(self, result):
        series, anomalies = result
        sc = self.scenario
        for s in series:
            u = node("met", "series", sc, s["app_uri"], s["key"])
            yield u, RDF.type, OPS.MetricSeries
            yield u, MET.metricName, string(s["key"].split("|", 1)[0])
            if s["app_uri"]:
                yield u, OPS.onApplication, URIRef(s["app_uri"])
            if s["n"] is not None:
                yield u, OPS["count"], integer(s["n"])
            if s["t_start"] is not None:
                yield u, OPS.windowStart, ts(s["t_start"])
                yield u, OPS.windowEnd, ts(s["t_end"])
            for x in s["selectors"]:
                yield u, OPS.rawSelector, string(x)
        for a in anomalies:
            u = node("met", "anomaly", sc, a["app_uri"], a["series_key"], a["t_start"])
            yield u, RDF.type, OPS.Anomaly
            yield u, OPS.onSeries, node("met", "series", sc, a["app_uri"], a["series_key"])
            if a["app_uri"]:
                yield u, OPS.onApplication, URIRef(a["app_uri"])
            yield u, OPS.atTime, ts(a["t_start"])
            yield u, OPS.windowStart, ts(a["t_start"])
            if a["t_end"] is not None:
                yield u, OPS.windowEnd, ts(a["t_end"])
            if finite(a["magnitude"]):
                yield u, OPS.magnitude, dec(a["magnitude"])
            if a["direction"]:
                yield u, MET.direction, string(a["direction"])
            yield u, OPS.rawSelector, string(a["selector"])
