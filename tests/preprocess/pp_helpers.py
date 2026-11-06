"""Helpers over a preprocessed tree (tests only; they may read _answers/)."""
import json

import polars as pl


def table(p, name: str) -> pl.DataFrame:
    return pl.read_parquet(p.proc / f"{name}.parquet")


def quarantine(p) -> pl.DataFrame:
    return table(p, "quarantine")


def skew_table(p) -> dict[str, int]:
    return json.loads((p.proc / "clock_skew.json").read_text())["logs"]


def manifest(p) -> dict:
    return json.loads((p.answers / "hero.manifest.json").read_text())


def injections(p, injector_id: str) -> list[dict]:
    return [dict(i, detail=json.loads(i["detail"])) for i in manifest(p)["injections"]
            if i["injector_id"] == injector_id]


def injected_skew_host(p) -> str:
    (inj,) = injections(p, "log.host_skew")
    return inj["detail"]["host"]


def derived(p, name: str) -> pl.DataFrame:
    return pl.read_parquet(p.proc / "derived" / f"{name}.parquet")


def app_uri(app: str) -> str:
    return f"https://kg.local/app/application/{app}"
