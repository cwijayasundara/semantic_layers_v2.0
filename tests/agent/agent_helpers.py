import json

from e2ekg.graph.store import Store


def copy_store(src: Store) -> Store:
    dst = Store(None)
    dst.oxigraph.extend(src.oxigraph)
    return dst


def call(tools, name: str, **args):
    return json.loads(tools[name].invoke(args))
