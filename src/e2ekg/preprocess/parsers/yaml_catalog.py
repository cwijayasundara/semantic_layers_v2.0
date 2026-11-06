"""Multi-document YAML service catalog: a record is one document including its
`---` line (R13)."""
import re
from collections.abc import Iterator

import yaml

from e2ekg.preprocess.parsers._base import Unparseable, decode, row, split_known, split_on_header

HEADER = re.compile(rb"^---[ \t]*$", re.M)
_KEYS = frozenset({"id", "aliases", "owner_team", "tier", "regions", "kind", "dependencies"})


class YamlCatalogParser:
    name = "yaml_catalog"
    source = "catalog"

    def split(self, data: bytes) -> Iterator[tuple[int, int]]:
        return split_on_header(data, HEADER)

    def parse(self, payload: bytes) -> dict:
        try:
            d = yaml.safe_load(decode(payload))
        except yaml.YAMLError as e:
            raise Unparseable(f"yaml: {e}") from e
        if not isinstance(d, dict) or not isinstance(d.get("id"), str):
            raise Unparseable("yaml: catalog document needs a string `id`")
        fields, residual = split_known(d, _KEYS)
        return row(None, d["id"], fields, residual, app_id=d["id"], app_kind=d.get("kind"))


YAML_CATALOG = YamlCatalogParser()
