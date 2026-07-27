"""Structural invariants over the graph. Things that should never be true.

An invariant here is not a threshold on a metric. Metrics answer "is the model
behaving differently than it used to", which is a question about the model.
These answer "is the model wired to something it should never have been wired
to", which is a question about the graph, and no amount of watching predictions
will surface it. The demo case is the sharp version of that: a feature arrives
that must not be used, and accuracy improves, so every distribution monitor in
the building reports healthy.

Implemented so far:

  protected attribute reaches a deployed model
      A column tagged protected_attribute flows into a feature of a model the
      registry says is deployed. Both halves matter. A protected column sitting
      in a warehouse table is normal and is not a finding. The same column
      reaching a live decision is the thing regulation cares about, and the only
      artefact that knows the difference is the column graph.

Deployment is read from the registry rather than inferred. The PROD in a model
urn is the fabric the entity lives in, not a statement that anything is serving,
and treating it as one would report every registered experiment as deployed. The
mlflow connector writes the registry stage as a tag, so mlflow_production means
someone actually promoted it.

    python tools/sentinel.py
    python tools/sentinel.py --fail-on-violation
"""

from __future__ import annotations

import argparse
import json
import sys

from graph import find, gql
from trace import columns, entities, protected_reaching, training_datasets

INVARIANT = "protected attribute reaches a deployed model"

# registry stages that mean something is serving. mlflow writes the stage as a
# tag, lowercased, so None becomes mlflow_none and Production mlflow_production.
DEPLOYED_STAGES = ("mlflow_production", "mlflow_staging")

_MODEL = """
query($urn: String!) {
  mlModel(urn: $urn) {
    urn
    name
    tags { tags { tag { urn } } }
  }
}
"""


def _stages(model: dict) -> set[str]:
    return {
        t["tag"]["urn"].rsplit(":", 1)[-1]
        for t in ((model.get("tags") or {}).get("tags") or [])
    }


def deployed_models() -> list[dict]:
    """Every model the registry says is serving, with the stage that says so."""
    out: list[dict] = []
    for hit in find("*", types=["MLMODEL"], count=200):
        model = gql(_MODEL, {"urn": hit["urn"]}).get("mlModel") or {}
        if not model:
            continue
        serving = _stages(model) & set(DEPLOYED_STAGES)
        if serving:
            out.append({"urn": model["urn"], "name": model.get("name") or model["urn"],
                        "stages": sorted(serving)})
    return out


def unresolved_hops(table: str) -> list[str]:
    """Upstream tables named by an edge but carrying no entity.

    dbt sometimes names a source in the consuming model's schema rather than its
    own, which leaves a urn no connector ever wrote. Nothing there has columns or
    tags, so a lookup against it returns a confident nothing. Today every such
    node has a real twin reached in parallel, but that is luck rather than
    design, and if it ever stops being true a protected column could go unseen.
    """
    from trace import _edges

    missing: set[str] = set()
    walked: set[str] = {table}
    frontier = [table]
    while frontier:
        nxt: list[str] = []
        for name in frontier:
            for up_name, _, down_name, _ in _edges(name):
                if down_name != name or up_name in walked:
                    continue
                walked.add(up_name)
                nxt.append(up_name)
                if not entities(up_name):
                    missing.add(up_name)
        frontier = nxt
    return sorted(missing)


def violations() -> tuple[list[dict], list[dict]]:
    """Findings and warnings for the one invariant implemented so far."""
    findings: list[dict] = []
    warnings: list[dict] = []

    for model in deployed_models():
        trained_on = training_datasets(model["urn"])
        if not trained_on:
            warnings.append({
                "model": model["urn"],
                "warning": "deployed but no training dataset on its lineage, "
                           "so nothing can be checked about what it learned from",
            })
            continue
        for table in trained_on:
            for phantom in unresolved_hops(table):
                warnings.append({
                    "model": model["urn"],
                    "table": phantom,
                    "warning": f"{phantom} is named by a column edge but has no entity, "
                               f"so its tags cannot be read",
                })
            for hit in protected_reaching(table):
                findings.append({
                    "invariant": INVARIANT,
                    "model": model["urn"],
                    "model_name": model["name"],
                    "stages": model["stages"],
                    "table": table,
                    "feature": hit["column"],
                    "origin_table": hit["source"],
                    "origin_column": hit["source_column"],
                    "hops": hit["hops"],
                    "tags": hit["tags"],
                })
    return findings, warnings


def _report(findings: list[dict], warnings: list[dict]) -> None:
    models = sorted({f["model_name"] for f in findings})
    print(f"== {INVARIANT} ==")
    if not findings:
        print("  no violations")
    for name in models:
        mine = [f for f in findings if f["model_name"] == name]
        stages = ", ".join(mine[0]["stages"])
        print(f"\n  {name}  ({stages})")
        print(f"  trained on {mine[0]['table']}, "
              f"{len(columns(mine[0]['table']))} columns, {len(mine)} protected")
        for f in mine:
            if f["hops"]:
                origin = f"{f['origin_table']}.{f['origin_column']}, {f['hops']} hops back"
            else:
                origin = "tagged on the feature itself"
            print(f"    {f['feature']:24} {origin}")
            print(f"    {'':24} [{', '.join(f['tags'])}]")

    if warnings:
        print("\n== warnings ==")
        for w in warnings:
            print(f"  {w['warning']}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="emit findings as json")
    ap.add_argument("--fail-on-violation", action="store_true",
                    help="exit non zero when anything fires, for use in a pipeline")
    args = ap.parse_args()

    findings, warnings = violations()
    if args.json:
        print(json.dumps({"findings": findings, "warnings": warnings}, indent=2))
    else:
        _report(findings, warnings)

    return 1 if (findings and args.fail_on_violation) else 0


if __name__ == "__main__":
    sys.path.insert(0, __file__.rsplit("/", 1)[0].rsplit("\\", 1)[0])
    raise SystemExit(main())
