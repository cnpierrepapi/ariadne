"""Blast radius. Before a change lands, say what it can reach.

Root cause is the glamorous half and the wrong half to reach for first. By the
time anyone runs it the model is already serving decisions off the thing nobody
meant to add. The cheap moment is the pull request, when the change is still a
diff and the only cost of knowing is a comment.

The question this answers is narrow on purpose. Not "is this change safe", which
nobody can answer, but "what does this column reach, and is any of it deployed".
Both halves come from the graph, so both are checkable rather than argued.

Two things make the answer worth reading over a plain downstream list:

  it separates deployed from registered. A dbt model with forty downstream
  dependencies of which none are serving is a different change from one with a
  single downstream that is. The registry stage tag says which, so the answer
  ranks by consequence rather than by count.

  it carries the governance verdict for the column. If the column being changed
  is a restricted basis under the regime in force, that is the sentence that
  belongs at the top of the comment, not buried under a dependency tree.

Reads DataHub through the Agent Context Kit or the MCP server, never directly.

    python tools/blast.py analytics_marts.dim_person
    python tools/blast.py analytics_marts.dim_person --column public_coverage_flag
    python tools/blast.py analytics_marts.dim_person --column public_coverage_flag \
        --policy eu_ai_act --comment
"""

from __future__ import annotations

import argparse
import json
import sys

from context import add_transport_arg, open_context, resolve_dataset
from policy import load as load_policy
from trace import protected_reaching

# registry stages that mean something is serving. mlflow writes the stage as a
# tag, lowercased, so Production becomes mlflow_production.
DEPLOYED_STAGES = {"mlflow_production", "mlflow_staging"}


def radius(ctx, table: str, column: str | None = None,
           max_hops: int = 5) -> dict:
    """Everything reachable downstream of a table, or of one column in it.

    A column walk cannot reach a model on its own, and that is a fact about the
    connectors rather than a gap in the question. dbt emits column level edges,
    so a column can be followed table to table. MLflow emits an edge from the
    training frame to the model with no column on it, because a training run
    consumes a table. So the walk runs in two stages: follow the column while
    there are column edges to follow, then ask each table it landed in what
    models it feeds. Skipping the second stage would report that a restricted
    column reaches nothing, which is the wrong answer delivered confidently.
    """
    urn = resolve_dataset(ctx, table)
    reached = ctx.lineage(urn, column=column, upstream=False, max_hops=max_hops)

    models, datasets, runs = [], [], []
    for node in reached:
        if node["type"] == "MLMODEL":
            models.append(node)
        elif node["type"] == "DATASET":
            datasets.append(node)
        else:
            runs.append(node)

    if column:
        seen = {m["urn"] for m in models}
        for landed in list(datasets):
            for node in ctx.lineage(landed["urn"], upstream=False,
                                    max_hops=max_hops):
                if node["type"] == "MLMODEL" and node["urn"] not in seen:
                    seen.add(node["urn"])
                    # hops counted from the column, not from the table the
                    # second stage started at, or the distance would reset
                    node = {**node, "degree": landed["degree"] + node["degree"],
                            "via": landed["name"]}
                    models.append(node)
                elif node["type"] not in ("MLMODEL", "DATASET"):
                    runs.append(node)

    models = [{**m, "serving": sorted(set(m["tags"]) & DEPLOYED_STAGES)}
              for m in models]

    # deployed first, then by how far away, because a serving model six hops out
    # still matters more than a scratch table one hop out
    models.sort(key=lambda m: (not m["serving"], m["degree"], m["name"] or ""))
    return {"table": table, "column": column, "urn": urn,
            "datasets": datasets, "models": models, "runs": runs,
            "deployed": [m for m in models if m["serving"]]}


CATALOG = "warehouse"


def verdict(table: str, column: str | None, regime: str | None) -> dict | None:
    """What the policy in force says about the column being changed.

    The catalog prefix has to be tried both ways. Lineage and the dbt manifest
    name a table `analytics_marts.dim_person`, while the tag lookup keys on the
    fully qualified `warehouse.analytics_marts.dim_person`, and asking with the
    wrong one returns an empty list rather than an error. An empty list here
    reads as "this column is not restricted", which is the most expensive wrong
    answer this tool can give, so both spellings are tried before believing it.
    """
    if not column:
        return None
    names = [table] if table.startswith(f"{CATALOG}.") else [table, f"{CATALOG}.{table}"]
    for name in names:
        for hit in protected_reaching(name, regime):
            if hit["column"] == column:
                return hit
    return None


def _basis_line(call: dict) -> str:
    """Why this column is in scope, in the words the regime itself would use.

    Two different routes in. A regime that names the attribute gives a named
    answer. A regime that watches a tag gives a reason instead, and printing the
    missing name rather than the reason turns the most interesting case, a proxy
    nobody declared, into the word None.
    """
    if call.get("attribute"):
        sentence = f"this column holds {call['attribute']}, a restricted basis"
    else:
        sentence = call.get("why") or "in scope for examination"
    return sentence.rstrip(". ") + "."


def _report(out: dict, call: dict | None, pol, via: str) -> None:
    target = f"{out['table']}.{out['column']}" if out["column"] else out["table"]
    print(f"== blast radius of a change to {target} ==")
    print(f"   read through {via}, under {pol.long_name}\n")

    if call:
        print(f"  {call['basis']:12} {_basis_line(call)}")
        print(f"  {'':12} under {call['citation']}\n")

    deployed = out["deployed"]
    if deployed:
        print(f"  reaches {len(deployed)} deployed model"
              f"{'s' if len(deployed) > 1 else ''}")
        for m in deployed:
            print(f"    {m['name']:26} {', '.join(m['serving'])}, "
                  f"{m['degree']} hops")
            for h in m["health"]:
                print(f"    {'':26} datahub already flags: {h}")
    else:
        print("  reaches no deployed model")

    other = [m for m in out["models"] if not m["serving"]]
    if other:
        print(f"\n  and {len(other)} registered but not serving")
        for m in other:
            print(f"    {m['name']:26} {', '.join(m['tags']) or 'no stage'}")

    print(f"\n  through {len(out['datasets'])} downstream table"
          f"{'s' if len(out['datasets']) != 1 else ''}")
    for d in out["datasets"]:
        carried = f" carrying {', '.join(d['columns'])}" if d["columns"] else ""
        print(f"    {d['degree']} hop  [{d['platform']}] {d['name']}{carried}")

    if out["runs"]:
        print(f"\n  and {len(out['runs'])} training run"
              f"{'s' if len(out['runs']) != 1 else ''} that consumed it")


def _comment(out: dict, call: dict | None, pol) -> str:
    """The same answer shaped for a pull request."""
    target = f"`{out['table']}.{out['column']}`" if out["column"] else f"`{out['table']}`"
    lines = [f"**Blast radius: {target}**", ""]

    if call:
        lines += [f"> **{call['basis']}**: {_basis_line(call)} Under "
                  f"{call['citation']}.", ""]

    deployed = out["deployed"]
    if deployed:
        lines.append(f"Reaches **{len(deployed)} deployed model"
                     f"{'s' if len(deployed) > 1 else ''}**:")
        lines.append("")
        lines.append("| model | stage | hops |")
        lines.append("| --- | --- | --- |")
        for m in deployed:
            lines.append(f"| `{m['name']}` | {', '.join(m['serving'])} | {m['degree']} |")
    else:
        lines.append("Reaches no deployed model.")
    lines.append("")

    if out["datasets"]:
        lines.append(f"<details><summary>{len(out['datasets'])} downstream tables"
                     f"</summary>\n")
        for d in out["datasets"]:
            carried = f" (carries `{', '.join(d['columns'])}`)" if d["columns"] else ""
            lines.append(f"- {d['degree']} hop, `{d['name']}` on {d['platform']}{carried}")
        lines.append("\n</details>")
        lines.append("")

    lines.append(f"<sub>Ariadne, from DataHub lineage, under {pol.long_name}.</sub>")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("table", help="the table being changed, e.g. analytics_marts.dim_person")
    ap.add_argument("--column", help="narrow to one column, so the walk follows "
                                     "column edges rather than table edges")
    ap.add_argument("--policy", help="regime to judge the column under")
    ap.add_argument("--max-hops", type=int, default=5)
    ap.add_argument("--comment", action="store_true",
                    help="emit a pull request comment instead of a report")
    ap.add_argument("--json", action="store_true")
    add_transport_arg(ap)
    args = ap.parse_args()

    pol = load_policy(args.policy)
    with open_context(args.via) as ctx:
        out = radius(ctx, args.table, args.column, args.max_hops)
    call = verdict(args.table, args.column, args.policy)

    if args.json:
        print(json.dumps({**out, "verdict": call, "regime": pol.name}, indent=2))
    elif args.comment:
        print(_comment(out, call, pol))
    else:
        _report(out, call, pol, args.via)

    # a change that reaches a deployed model is not a failure, it is a thing a
    # person should read. only a restricted column reaching one is worth a
    # non zero exit in a pipeline.
    blocking = call and call["basis"] == "prohibited" and out["deployed"]
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.path.insert(0, __file__.rsplit("/", 1)[0].rsplit("\\", 1)[0])
    raise SystemExit(main())
