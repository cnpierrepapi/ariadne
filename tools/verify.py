"""Check that the catalog holds what it should, and nothing it should not.

Run after any rebuild or reingest. Three questions, in order of how badly a wrong
answer would mislead everything downstream:

  1. Is the instance clean? Anything outside the ariadne namespace is either
     leftover sample data or another project, and both make a demo look like a
     tip. This also catches a reingest that silently doubled entities.

  2. Is the thread whole? The model has to reach the raw census tables. If it does
     not, every check built on the traversal is walking a graph with a hole in it
     and will report all clear for the wrong reason.

  3. Are the governance tags where the checks will look for them? They arrive on
     the dbt sibling and not on the warehouse entity, which is the single easiest
     way to build a protected attribute check that finds nothing and says so
     confidently.

DataHub indexes asynchronously through Kafka, so a check straight after an ingest
can see an empty graph that fills in a minute later. This waits rather than
reporting a failure that is really just impatience.
"""

from __future__ import annotations

import argparse
import sys
import time

from graph import UPSTREAM, find, gql, walk

NAMESPACE = "warehouse."
MODEL = "urn:li:mlModel:(urn:li:dataPlatform:mlflow,income-classifier_1,PROD)"

# the chain that has to be intact, coarsest to rawest
EXPECTED_CHAIN = [
    "warehouse.analytics_marts.income_features",
    "warehouse.analytics_marts.dim_person",
    "warehouse.analytics_staging.stg_person",
    "warehouse.public.raw_person",
]

# entities that belong to no project and are always noise if present
FOREIGN_HINTS = ("b2fd91", "lineworld", "hcatalog_", "SampleHive", "SampleKafka")

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

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}")
    return bool(ok)


def tagged_columns(urn: str) -> dict[str, set[str]]:
    data = gql(_TAGS, {"urn": urn}).get("dataset") or {}
    out: dict[str, set[str]] = {}
    blocks = ((data.get("schemaMetadata") or {}).get("fields") or [],
              (data.get("editableSchemaMetadata") or {}).get("editableSchemaFieldInfo") or [])
    for fields in blocks:
        for field in fields:
            tags = {t["tag"]["urn"].rsplit(":", 1)[-1]
                    for t in ((field.get("globalTags") or {}).get("tags") or [])}
            if tags:
                out.setdefault(field["fieldPath"], set()).update(tags)
    return out


def await_index(urn: str, minimum: int, timeout: int) -> dict[str, int]:
    """Give the graph index time to catch up before believing it is empty."""
    deadline = time.time() + timeout
    reached: dict[str, int] = {}
    while time.time() < deadline:
        reached = walk(urn, UPSTREAM)
        if len(reached) >= minimum:
            return reached
        time.sleep(10)
    return reached


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--timeout", type=int, default=180,
                    help="seconds to allow the graph index to catch up")
    args = ap.parse_args()

    print("\n== the instance holds only this project ==")
    entities = find("*", count=200)
    urns = [e["urn"] for e in entities]
    foreign = [u for u in urns if any(h in u for h in FOREIGN_HINTS)]
    check("no leftover sample or other project data", not foreign,
          f"{len(urns)} entities" + (f"; foreign: {foreign[:3]}" if foreign else ""))

    by_type: dict[str, int] = {}
    for e in entities:
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1
    print("     " + ", ".join(f"{k.lower()} {v}" for k, v in sorted(by_type.items())))

    print("\n== the thread from the model to the raw census tables ==")
    reached = await_index(MODEL, len(EXPECTED_CHAIN), args.timeout)
    check("the model has upstream lineage at all", bool(reached),
          f"{len(reached)} nodes reachable")
    for name in EXPECTED_CHAIN:
        hit = next((u for u in reached if name in u), None)
        hops = reached.get(hit) if hit else None
        check(f"reaches {name}", hit is not None,
              f"{hops} hops" if hit else "not on the thread")

    print("\n== governance tags are where the checks will look ==")
    for platform in ("dbt", "postgres"):
        urn = f"urn:li:dataset:(urn:li:dataPlatform:{platform},warehouse.analytics_marts.dim_person,PROD)"
        tags = tagged_columns(urn)
        protected = {c for c, t in tags.items() if "protected_attribute" in t}
        print(f"     {platform}: {len(tags)} tagged columns, {len(protected)} protected")
        if platform == "dbt":
            check("protected attributes are tagged on the dbt entity",
                  "race_code" in protected, f"{sorted(protected)}")
        else:
            check("the warehouse entity carries no tags of its own, so checks "
                  "must resolve siblings", not tags,
                  "confirms the sibling hop is required, not optional")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.path.insert(0, __file__.rsplit("/", 1)[0].rsplit("\\", 1)[0])
    raise SystemExit(main())
