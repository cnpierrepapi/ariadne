"""Ask the same question through both DataHub surfaces and check the answers match.

There are two supported ways for an agent to read a DataHub catalog, and Ariadne
wires both. The Agent Context Kit runs in process and is fast. The MCP server is
a child process spoken to in JSON-RPC, and is slow, and is the path a third party
agent would actually take.

Having two is only worth anything if they agree. A fast path that quietly returns
a shorter answer is worse than no fast path, because every check built on it
reports all clear on a smaller graph. So this runs the same walks down both and
compares what comes back.

It compares the parts a decision is made from: which nodes, how far away, and
which column carried the edge. It deliberately does not compare the raw payloads,
because the MCP server trims responses to fit a model's context window and the
kit does not, so those differ by design and comparing them would fail on nothing.

    python tools/agree.py
"""

from __future__ import annotations

import argparse
import sys

from context import open_context, resolve_dataset

FEATURES = "analytics_marts.workforce_features"
DIM = "analytics_marts.dim_person"

WALKS = [
    ("upstream of workforce_features on public_coverage_flag",
     FEATURES, {"column": "public_coverage_flag", "upstream": True, "max_hops": 8}),
    ("downstream of workforce_features",
     FEATURES, {"upstream": False, "max_hops": 5}),
    ("upstream of dim_person on disability_code",
     DIM, {"column": "disability_code", "upstream": True, "max_hops": 8}),
]


def fingerprint(nodes: list[dict]) -> list[tuple]:
    """The part of a walk a decision is actually made from."""
    return sorted((n["urn"], n["degree"], tuple(sorted(n["columns"] or [])))
                  for n in nodes)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    print("== do the two DataHub surfaces agree ==")

    # One transport at a time. Both were open at once in the first version and
    # the in process client started failing its searches while the MCP session
    # was live, which is a shared state problem in the client rather than a
    # disagreement about the graph. Running them in turn removes the question,
    # and a comparison that has to hold both open to work would be measuring the
    # wrong thing anyway.
    results = {}
    for via in ("kit", "mcp"):
        with open_context(via) as ctx:
            if via == "mcp":
                print(f"   mcp  {ctx.server} over stdio, {len(ctx.tools)} tools")
            else:
                print("   kit  datahub-agent-context, in process")
            results[via] = [
                fingerprint(ctx.lineage(resolve_dataset(ctx, table), **opts))
                for _, table, opts in WALKS
            ]

    print()
    failures = 0
    for i, (label, _, _) in enumerate(WALKS):
        left, right = results["kit"][i], results["mcp"][i]
        same = left == right
        failures += not same
        print(f"  {'agree' if same else 'DIFFER':7} {len(left):2} nodes  {label}")
        if not same and not args.quiet:
            for row in sorted(set(left) ^ set(right)):
                side = "kit only" if row in set(left) else "mcp only"
                print(f"          {side}: {row[0]} at {row[1]}")

    print(f"\n  {len(WALKS) - failures}/{len(WALKS)} walks identical through both")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.path.insert(0, __file__.rsplit("/", 1)[0].rsplit("\\", 1)[0])
    raise SystemExit(main())
