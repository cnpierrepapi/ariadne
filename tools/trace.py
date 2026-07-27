"""Follow a single column back through the warehouse to where it came from.

The coarse lineage in graph.py answers "what feeds this model". That is not enough
to say anything useful about a protected attribute, because the table that feeds
the model also sits downstream of a table holding every protected attribute there
is. dim_person carries race, sex, ancestry and six more. income_features is built
from dim_person and deliberately selects none of them. A check written against
table lineage cannot tell those two situations apart, so it would report a
violation today, when nothing is wrong, and keep reporting one forever.

Column lineage can tell them apart. The dbt connector parses the model SQL and
emits an edge per output column, so the question becomes whether race_code reaches
a column the model actually trains on, which is answerable and stays quiet until
it is true.

Two things about this graph make a naive walk give a confident wrong answer.

First, one table is several entities. dim_person exists as a dbt dataset and as a
postgres dataset, joined by a real siblings aspect, and income_features exists a
third time as an mlflow dataset, joined by an ordinary lineage edge instead. The
governance tags land on the dbt entity only. Traversal returns the postgres one.
Ask the postgres entity for its tags and it answers, truthfully, that it has none.

Second, the column edges zigzag between those entities. The edge out of
income_features is stored on the dbt entity and points at the postgres dim_person,
whose own edges are stored back on the dbt dim_person. Land on a postgres node,
read only that node's edges, and the walk stops early with no error.

Both problems dissolve if a node is a table name rather than an entity. Names here
are fully qualified catalog paths, so sharing one means being the same table. This
module resolves every entity that carries a name, unions their edges and their
tags, and walks over names. It also drops edges whose upstream and downstream are
the same table, which is how the siblings aspect represents the mirror between dbt
and postgres and is not a real hop.

    python tools/trace.py columns <dataset urn or name>
    python tools/trace.py column  <dataset urn or name> <column>
    python tools/trace.py model   <model urn>
"""

from __future__ import annotations

import argparse
import sys
from functools import lru_cache

from graph import UPSTREAM, find, gql, walk
from policy import Policy
from policy import load as load_policy

_POLICY: Policy | None = None


def policy(regime: str | None = None) -> Policy:
    """The regime in force. Loaded once, since the checks all share it."""
    global _POLICY
    if _POLICY is None or regime is not None:
        _POLICY = load_policy(regime)
    return _POLICY

_FINE = """
query($urn: String!) {
  dataset(urn: $urn) {
    fineGrainedLineages {
      upstreams { urn path }
      downstreams { urn path }
      transformOperation
    }
  }
}
"""

_TAGS = """
query($urn: String!) {
  dataset(urn: $urn) {
    schemaMetadata { fields { fieldPath globalTags { tags { tag { urn } } } } }
    editableSchemaMetadata {
      editableSchemaFieldInfo { fieldPath globalTags { tags { tag { urn } } } }
    }
  }
}
"""


def dataset_name(urn: str) -> str:
    """The table name inside a dataset urn, which is what siblings share."""
    if not urn.startswith("urn:li:dataset:("):
        return urn
    return urn[len("urn:li:dataset:("):-1].split(",")[1]


def platform(urn: str) -> str:
    if ":dataPlatform:" not in urn:
        return "?"
    return urn.split(":dataPlatform:")[1].split(",")[0]


@lru_cache(maxsize=1)
def _by_name() -> dict[str, tuple[str, ...]]:
    """Every dataset entity in the catalog, grouped by the table it describes."""
    grouped: dict[str, list[str]] = {}
    for entity in find("*", types=["DATASET"], count=500):
        grouped.setdefault(dataset_name(entity["urn"]), []).append(entity["urn"])
    return {name: tuple(sorted(urns)) for name, urns in grouped.items()}


def entities(name: str) -> tuple[str, ...]:
    """All urns describing one table. The tags and the edges are spread across them."""
    return _by_name().get(name, ())


def resolve(target: str) -> str:
    """Accept a urn or a bare table name and return the table name."""
    if target.startswith("urn:li:"):
        return dataset_name(target)
    if target in _by_name():
        return target
    matches = [n for n in _by_name() if target in n]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"no dataset matches {target!r}")
    raise SystemExit(f"{target!r} is ambiguous: {', '.join(sorted(matches))}")


@lru_cache(maxsize=None)
def _edges(name: str) -> tuple[tuple[str, str, str, str], ...]:
    """Column edges for a table, unioned over every entity describing it.

    Returned as (upstream table, upstream column, downstream table, downstream
    column). Edges inside one table are the siblings mirror, not a hop, so they
    are dropped here rather than at every call site.
    """
    out: set[tuple[str, str, str, str]] = set()
    for urn in entities(name):
        data = gql(_FINE, {"urn": urn}).get("dataset") or {}
        for edge in data.get("fineGrainedLineages") or []:
            for down in edge.get("downstreams") or []:
                for up in edge.get("upstreams") or []:
                    up_name, down_name = dataset_name(up["urn"]), dataset_name(down["urn"])
                    if up_name == down_name:
                        continue
                    out.add((up_name, up["path"], down_name, down["path"]))
    return tuple(sorted(out))


@lru_cache(maxsize=None)
def tags(name: str) -> dict[str, frozenset[str]]:
    """Column tags for a table, unioned over every entity describing it.

    This union is the whole point. The dbt entity holds the governance tags and
    the postgres entity holds none, and traversal hands you the postgres one.
    """
    found: dict[str, set[str]] = {}
    for urn in entities(name):
        data = gql(_TAGS, {"urn": urn}).get("dataset") or {}
        blocks = (
            (data.get("schemaMetadata") or {}).get("fields") or [],
            (data.get("editableSchemaMetadata") or {}).get("editableSchemaFieldInfo") or [],
        )
        for fields in blocks:
            for field in fields:
                applied = {
                    t["tag"]["urn"].rsplit(":", 1)[-1]
                    for t in ((field.get("globalTags") or {}).get("tags") or [])
                }
                if applied:
                    found.setdefault(field["fieldPath"], set()).update(applied)
    return {column: frozenset(applied) for column, applied in found.items()}


def columns(name: str) -> list[str]:
    """Column names for a table, unioned over every entity describing it."""
    query = """
    query($urn: String!) {
      dataset(urn: $urn) { schemaMetadata { fields { fieldPath } } }
    }
    """
    seen: list[str] = []
    for urn in entities(name):
        data = gql(query, {"urn": urn}).get("dataset") or {}
        for field in (data.get("schemaMetadata") or {}).get("fields") or []:
            if field["fieldPath"] not in seen:
                seen.append(field["fieldPath"])
    return seen


def parents(name: str, column: str) -> set[tuple[str, str]]:
    """The columns one hop upstream of this one."""
    return {
        (up_name, up_col)
        for up_name, up_col, down_name, down_col in _edges(name)
        if down_name == name and down_col == column
    }


def ancestry(name: str, column: str, depth: int = 12) -> dict[tuple[str, str], int]:
    """Every column this one is derived from, mapped to how many hops back it is."""
    seen: dict[tuple[str, str], int] = {(name, column): 0}
    frontier = [(name, column)]
    for hop in range(1, depth + 1):
        nxt: list[tuple[str, str]] = []
        for node in frontier:
            for parent in parents(*node):
                if parent not in seen:
                    seen[parent] = hop
                    nxt.append(parent)
        if not nxt:
            break
        frontier = nxt
    seen.pop((name, column), None)
    return seen


def training_datasets(model_urn: str) -> list[str]:
    """The tables a model was trained on, nearest first.

    The model does not point at a dataset directly. It points at the training run,
    which points at the table, so take the closest datasets on the upstream walk
    rather than assuming a fixed number of hops.
    """
    reached = walk(model_urn, UPSTREAM)
    datasets = [(hop, urn) for urn, hop in reached.items() if urn.startswith("urn:li:dataset:(")]
    if not datasets:
        return []
    nearest = min(hop for hop, _ in datasets)
    names: list[str] = []
    for hop, urn in sorted(datasets):
        if hop == nearest and dataset_name(urn) not in names:
            names.append(dataset_name(urn))
    return names


def _describe(pol: Policy, column: str, applied: frozenset[str]) -> dict:
    """What the regime in force says about a column, if it says anything.

    A column can be in scope because the regime declares the attribute it holds,
    or only because it carries a tag the regime watches. The second case is not
    weaker, it is how a proxy gets caught: a neighbourhood identifier is tagged as
    personal data and named by no statute, and under a regime that asks for
    examination rather than listing forbidden columns it still has to be looked at.
    """
    spec = pol.by_column().get(column)
    if spec:
        return {
            "attribute": spec["attribute"],
            "basis": spec["basis"],
            "citation": spec.get("citation", pol.citation),
        }
    return {
        "attribute": None,
        "basis": "examine",
        "citation": pol.citation,
        "why": f"in scope because it carries {', '.join(sorted(applied))}, "
               f"and {pol.name} watches that tag",
    }


def protected_reaching(name: str, regime: str | None = None) -> list[dict]:
    """Restricted columns that actually reach a column of this table.

    Reaching is the whole question. Every table downstream of dim_person has nine
    protected attributes somewhere in its history. Only some of them select one.

    What counts as restricted comes from the regime, not from this file. ECOA
    watches columns tagged as protected attributes. The EU AI Act asks for
    examination of biases affecting fundamental rights, so it watches personal data
    too, and the same warehouse yields more findings under it without a line here
    changing.
    """
    pol = policy(regime)
    findings: list[dict] = []
    for column in columns(name):
        own = tags(name).get(column, frozenset())
        if pol.is_in_scope(own):
            findings.append({
                "column": column, "source": name, "source_column": column,
                "hops": 0, "tags": sorted(own), **_describe(pol, column, own),
            })
            continue
        for (up_name, up_col), hop in sorted(ancestry(name, column).items(), key=lambda kv: kv[1]):
            inherited = tags(up_name).get(up_col, frozenset())
            if pol.is_in_scope(inherited):
                findings.append({
                    "column": column, "source": up_name, "source_column": up_col,
                    "hops": hop, "tags": sorted(inherited),
                    **_describe(pol, up_col, inherited),
                })
                break
    return findings


def _print_columns(name: str) -> None:
    marked = tags(name)
    print(f"{name}  ({', '.join(platform(u) for u in entities(name))})")
    for column in columns(name):
        applied = sorted(marked.get(column, ()))
        note = f"   [{', '.join(applied)}]" if applied else ""
        print(f"  {column}{note}")


def _print_column(name: str, column: str) -> None:
    if column not in columns(name):
        raise SystemExit(f"{name} has no column {column!r}")
    print(f"{name}.{column}")
    history = ancestry(name, column)
    if not history:
        print("  no upstream columns, this is where it enters the warehouse")
        return
    for (up_name, up_col), hop in sorted(history.items(), key=lambda kv: (kv[1], kv[0])):
        applied = sorted(tags(up_name).get(up_col, ()))
        note = f"   [{', '.join(applied)}]" if applied else ""
        print(f"  {hop} hop{'s' if hop > 1 else ' '}  {up_name}.{up_col}{note}")


def _print_model(model_urn: str) -> None:
    trained_on = training_datasets(model_urn)
    if not trained_on:
        raise SystemExit(f"no training dataset on the upstream lineage of {model_urn}")
    print(f"{model_urn.split(',')[1]} trained on {', '.join(trained_on)}")
    for name in trained_on:
        findings = protected_reaching(name)
        print(f"\n  {name}: {len(columns(name))} columns, "
              f"{len(findings)} carrying a protected attribute")
        for finding in findings:
            origin = (f"{finding['source']}.{finding['source_column']} "
                      f"({finding['hops']} hops back)") if finding["hops"] else "tagged here"
            print(f"    {finding['column']}  from {origin}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_columns = sub.add_parser("columns", help="columns of a table, with their tags")
    p_columns.add_argument("dataset")

    p_column = sub.add_parser("column", help="where one column came from")
    p_column.add_argument("dataset")
    p_column.add_argument("column")

    p_model = sub.add_parser("model", help="protected attributes reaching a model")
    p_model.add_argument("urn")

    args = ap.parse_args()

    if args.cmd == "columns":
        _print_columns(resolve(args.dataset))
    elif args.cmd == "column":
        _print_column(resolve(args.dataset), args.column)
    else:
        _print_model(args.urn)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, __file__.rsplit("/", 1)[0].rsplit("\\", 1)[0])
    raise SystemExit(main())
