"""Ask DataHub what it actually knows. A thin, honest window on the context graph.

Every claim Ariadne makes is a claim about this graph, so there needs to be one
place that reads it without interpretation. Used during the build to verify that
lineage landed, and used by the checks later to walk it.

    python tools/graph.py find income_features
    python tools/graph.py up   <urn>
    python tools/graph.py down <urn>
    python tools/graph.py thread <urn>       full chain, both directions
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

GMS = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")

UPSTREAM = "UPSTREAM"
DOWNSTREAM = "DOWNSTREAM"


def gql(query: str, variables: dict | None = None) -> dict:
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        f"{GMS}/api/graphql", data=payload,
        headers={"Content-Type": "application/json"},
    )
    token = os.environ.get("DATAHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"graphql {exc.code}: {exc.read()[:400].decode(errors='replace')}")
    if body.get("errors"):
        raise SystemExit("graphql errors: " + json.dumps(body["errors"])[:600])
    return body.get("data") or {}


_SEARCH = """
query($q: String!, $types: [EntityType!], $count: Int!) {
  searchAcrossEntities(input: {query: $q, types: $types, start: 0, count: $count}) {
    total
    searchResults { entity { urn type } }
  }
}
"""

# `lineage` hangs off the EntityWithRelationships interface, not off Entity, and
# asking for `name` across several concrete types trips a nullability conflict. So
# ask the interface for lineage only and read names out of the urns instead.
_LINEAGE = """
query($urn: String!, $direction: LineageDirection!, $count: Int!) {
  entity(urn: $urn) {
    urn
    type
    ... on EntityWithRelationships {
      lineage(input: {direction: $direction, start: 0, count: $count}) {
        total
        relationships { type entity { urn type } }
      }
    }
  }
}
"""


def find(query: str, types: list[str] | None = None, count: int = 50) -> list[dict]:
    data = gql(_SEARCH, {"q": query, "types": types, "count": count})
    hits = data.get("searchAcrossEntities") or {}
    return [r["entity"] for r in hits.get("searchResults", [])]


def lineage(urn: str, direction: str, count: int = 100) -> dict:
    data = gql(_LINEAGE, {"urn": urn, "direction": direction, "count": count})
    return data.get("entity") or {}


def neighbours(urn: str, direction: str) -> list[str]:
    entity = lineage(urn, direction)
    rels = (entity.get("lineage") or {}).get("relationships", [])
    return [r["entity"]["urn"] for r in rels]


def walk(urn: str, direction: str, depth: int = 8) -> dict[str, int]:
    """Every node reachable from `urn`, mapped to how many hops away it is."""
    seen: dict[str, int] = {urn: 0}
    frontier = [urn]
    for hop in range(1, depth + 1):
        nxt: list[str] = []
        for node in frontier:
            for other in neighbours(node, direction):
                if other not in seen:
                    seen[other] = hop
                    nxt.append(other)
        if not nxt:
            break
        frontier = nxt
    seen.pop(urn, None)
    return seen


def _short(urn: str) -> str:
    """A urn trimmed to what a person needs to read it."""
    if urn.startswith("urn:li:dataset:("):
        inner = urn[len("urn:li:dataset:("):-1]
        parts = inner.split(",")
        platform = parts[0].rsplit(":", 1)[-1]
        return f"[{platform}] {parts[1]}"
    kind = urn.split(":")[2]
    return f"[{kind}] {urn.split(':', 3)[-1].strip('()')}"


def _print_side(urn: str, direction: str, label: str) -> None:
    reached = walk(urn, direction)
    if not reached:
        print(f"  {label}: none")
        return
    print(f"  {label}: {len(reached)}")
    for node, hop in sorted(reached.items(), key=lambda kv: (kv[1], kv[0])):
        print(f"    {hop} hop{'s' if hop > 1 else ' '}  {_short(node)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_find = sub.add_parser("find", help="search entities")
    p_find.add_argument("query")
    p_find.add_argument("--type", action="append", dest="types")

    for name in ("up", "down", "thread"):
        p = sub.add_parser(name)
        p.add_argument("urn")

    args = ap.parse_args()

    if args.cmd == "find":
        hits = find(args.query, args.types)
        if not hits:
            print("nothing found")
            return 1
        for e in hits:
            print(f"{e['type']:22} {e['urn']}")
        return 0

    entity = lineage(args.urn, UPSTREAM)
    if not entity:
        print(f"no such entity: {args.urn}", file=sys.stderr)
        return 1
    print(_short(args.urn))
    if args.cmd in ("up", "thread"):
        _print_side(args.urn, UPSTREAM, "upstream")
    if args.cmd in ("down", "thread"):
        _print_side(args.urn, DOWNSTREAM, "downstream")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
