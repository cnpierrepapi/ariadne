"""Root cause. The number moved, so say what moved it.

Every ML observability tool on the market can tell you a model changed. That is
the easy half and it is where they stop, because a distribution plot has no way
to reach the pull request that caused it. The answer a person actually needs is
a sentence with a column, a file and a time in it.

Ariadne can produce that sentence because it holds both ends. The measurement
side knows the number moved and by how much against its own noise. The graph
side knows which columns the deployed model gained between those two moments and
where each one came from. Joining them is the whole agent.

The order matters and is deliberate. It starts from the measurement, not from
the diff, because plenty of columns get added that change nothing and reporting
them all would be a changelog rather than a cause. It ends at a raw column and a
dbt file, not at the feature table, because the feature table is where the
problem was noticed and not where it was introduced.

What it will not do is rank causes by plausibility when it cannot tell them
apart. If two columns arrived together it names both and says so. A confident
single answer would be more satisfying to read and would be invented.

Reads DataHub through the Agent Context Kit or the MCP server, never directly.

    python tools/rootcause.py --model workforce-classifier
    python tools/rootcause.py --model workforce-classifier --via mcp
"""

from __future__ import annotations

import argparse
import json
import sys

from context import add_transport_arg, open_context, resolve_dataset
from history import compare, for_model, load
from policy import load as load_policy
from trace import protected_reaching

CATALOG = "warehouse"


def origin(ctx, table: str, column: str, max_hops: int = 6) -> dict:
    """Walk one column of the feature table back to where it enters the warehouse.

    The furthest node the walk reaches is the origin. Ties are possible when a
    column is fed by more than one raw table, and the answer keeps all of them
    rather than choosing.

    Nothing here filters on the column name, and that is the important part. A
    warehouse renames as it cleans: this column is public_coverage_flag from
    staging onward and pubcov in the raw census extract. Keeping only nodes
    still carrying the name looks like a sensible guard, and it drops the origin
    while leaving a shorter answer that reads as complete. DataHub has already
    scoped the walk to this column's lineage, so every node it returns is on the
    path by construction and a name check can only lose the rename.
    """
    urn = resolve_dataset(ctx, table)
    walked = ctx.lineage(urn, column=column, upstream=True, max_hops=max_hops)
    if not walked:
        return {"column": column, "hops": None, "sources": [], "files": [],
                "renamed_to": None, "path": []}
    furthest = max(n["degree"] for n in walked)
    sources = [n for n in walked if n["degree"] == furthest]
    # what the column is called where it enters, if that is not what it is
    # called where it was noticed
    entry_names = sorted({c for n in sources for c in (n["columns"] or [])})
    return {
        "column": column,
        "hops": furthest,
        "sources": sorted({f"{n['platform']}:{n['name']}" for n in sources}),
        # the dbt sibling knows the file, the postgres one holds the data
        "files": sorted({n["file"] for n in walked if n.get("file")}),
        "renamed_to": [c for c in entry_names if c != column],
        "path": [{"degree": n["degree"], "platform": n["platform"],
                  "name": n["name"], "columns": n["columns"]} for n in walked],
    }


def investigate(ctx, model_name: str, regime: str | None = None) -> dict:
    recordings = for_model(load(), model_name)
    if len(recordings) < 2:
        raise SystemExit(
            f"{model_name} has {len(recordings)} recording(s). Root cause compares "
            f"a model against its own previous state, so it needs at least two."
        )
    before, after = recordings[-2], recordings[-1]

    shifts, changed = compare(before, after)
    gained, lost = changed["features_gained"], changed["features_lost"]

    table = after.get("feature_table") or f"analytics_marts.{model_name.split('-')[0]}_features"
    suspects = []
    for column in gained:
        found = origin(ctx, table, column)
        found["verdict"] = _verdict(table, column, regime)
        suspects.append(found)

    return {
        "model": model_name,
        "from": {"version": before.get("version"), "at": before["recorded_at"],
                 "accuracy": before.get("model_accuracy")},
        "to": {"version": after.get("version"), "at": after["recorded_at"],
               "accuracy": after.get("model_accuracy")},
        "table": table,
        "gained": gained, "lost": lost,
        "shifts": shifts, "suspects": suspects,
    }


def _verdict(table: str, column: str, regime: str | None) -> dict | None:
    names = [table] if table.startswith(f"{CATALOG}.") else [table, f"{CATALOG}.{table}"]
    for name in names:
        for hit in protected_reaching(name, regime):
            if hit["column"] == column:
                return hit
    return None


def _report(out: dict, pol, via: str) -> None:
    print(f"== why did {out['model']} move ==")
    print(f"   read through {via}, under {pol.long_name}")
    print(f"   comparing version {out['from']['version']} "
          f"({out['from']['at'][:19]}) with version {out['to']['version']} "
          f"({out['to']['at'][:19]})\n")

    acc_before, acc_after = out["from"]["accuracy"], out["to"]["accuracy"]
    if acc_before is not None and acc_after is not None:
        direction = "up" if acc_after > acc_before else "down"
        print(f"  accuracy went {direction}, {acc_before:.4f} to {acc_after:.4f} "
              f"({acc_after - acc_before:+.4f})")
        if direction == "up":
            print("  so nothing that watches model quality had a reason to object\n")

    if not out["shifts"]:
        print("  nothing moved by more than its own measurement noise")
        return

    print(f"  {len(out['shifts'])} measurement"
          f"{'s' if len(out['shifts']) > 1 else ''} moved beyond noise")
    for s in out["shifts"]:
        print(f"    {s['attribute']} ({s['group']} against {s['against']})  "
              f"{s['was']:.4f} to {s['now']:.4f}  {s['delta']:+.4f}, "
              f"{s['multiples_of_noise']:.0f}x the noise")

    print(f"\n  the deployed model gained {len(out['gained'])} column"
          f"{'s' if len(out['gained']) != 1 else ''} between those two moments")
    for s in out["suspects"]:
        print(f"\n    {s['column']}")
        if s["verdict"]:
            v = s["verdict"]
            label = v["attribute"] or "in scope by tag"
            print(f"      {v['basis']}, {label}, {v['citation']}")
        if s["hops"] is None:
            print("      no column edge leads back from it, so its origin is "
                  "not in the graph")
            continue
        print(f"      enters at {', '.join(s['sources'])}, {s['hops']} hops back")
        if s["renamed_to"]:
            print(f"      called {', '.join(s['renamed_to'])} where it enters, "
                  f"so a search by name would not have found it")
        for f in s["files"]:
            print(f"      defined in {f}")

    if out["lost"]:
        print(f"\n  and dropped {', '.join(out['lost'])}")

    single = len(out["suspects"]) == 1
    print(f"\n  {'cause' if single else 'candidates'}: "
          f"{', '.join(s['column'] for s in out['suspects']) or 'none found'}")
    if not single and out["suspects"]:
        print("  more than one column arrived together, so which of them moved "
              "the number cannot be settled from the graph alone")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True)
    ap.add_argument("--policy")
    ap.add_argument("--json", action="store_true")
    add_transport_arg(ap)
    args = ap.parse_args()

    pol = load_policy(args.policy)
    with open_context(args.via) as ctx:
        out = investigate(ctx, args.model, args.policy)

    if args.json:
        print(json.dumps({**out, "regime": pol.name}, indent=2))
    else:
        _report(out, pol, args.via)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, __file__.rsplit("/", 1)[0].rsplit("\\", 1)[0])
    raise SystemExit(main())
