"""The harvest's enrichers, in run order (Task 13 appends correlation)."""
from e2ekg.graph.enrichers.alerts_incidents import AlertsIncidentsEnricher
from e2ekg.graph.enrichers.catalog import CatalogEnricher
from e2ekg.graph.enrichers.changes import ChangesEnricher
from e2ekg.graph.enrichers.chat import ChatEnricher
from e2ekg.graph.enrichers.history import HistoryEnricher
from e2ekg.graph.enrichers.logs import LogPatternEnricher
from e2ekg.graph.enrichers.metrics import MetricAnomalyEnricher
from e2ekg.graph.enrichers.topology import TopologyEnricher
from e2ekg.graph.enrichers.traces import TraceFingerprintEnricher

ENRICHERS = (CatalogEnricher, TopologyEnricher, ChangesEnricher, MetricAnomalyEnricher,
             LogPatternEnricher, TraceFingerprintEnricher, AlertsIncidentsEnricher, ChatEnricher,
             HistoryEnricher)
