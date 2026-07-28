"""Work out what a column holds without being told, cheapest question first.

Everything else in Ariadne assumes somebody tagged the warehouse. That assumption
is the weakest part of the whole thing. Governance tags exist because a person sat
down and wrote them, and the columns nobody thought about are exactly the columns
that cause the trouble.

So this asks the question the other way round. Given a table and no tags at all,
which of these columns hold a human attribute?

Three rungs, cheapest first, and the escalation is the point:

  1  the name          string comparison, free, and wrong the moment somebody
                       renames a column or the source ships codebook names
  2  the values        what codes appear and in what proportion, one query, works
                       on a column called `q4_resp` as well as one called `sex`
  3  proxy scoring     fit a model, expensive, and the only rung that finds a
                       column carrying an attribute it does not contain

Rungs 1 and 2 live here. Rung 3 is reconstruct.py, and this module says when to go
there rather than pretending it can answer.

WHAT RUNG 2 IS ALLOWED TO KNOW. Only facts about the coding, never facts about
this warehouse. The declared code set for disability is 1 and 2 whichever database
it lands in, so matching on it is fair. Reading the column name out of
attributes.yml and calling that a discovery would be circular, and the test below
would pass while proving nothing.

    python tools/identify.py analytics_marts.dim_person
    python tools/identify.py public.raw_person          # opaque census names
    python tools/identify.py public.raw_person --json
"""

from __future__ import annotations

import argparse
import json
import os
import re

import psycopg2

from policy import attributes

WAREHOUSE = os.environ.get(
    "ARIADNE_WAREHOUSE_URL", "postgresql://ariadne:ariadne@localhost:5433/warehouse"
)

# A column with more distinct values than this is not a small coded category, so
# the code set comparison has nothing to say about it. Kept generous: PUMS place of
# birth and ancestry both run to a few hundred codes.
MAX_CODES = 600

# How much of a column may sit outside the declared code set before the match is
# refused. Real columns carry nulls and the odd reserved code, so demanding an
# exact set match rejects columns that plainly are the attribute.
TOLERANCE = 0.02


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def name_aliases(attribute: str, spec: dict) -> set[str]:
    """What this attribute might plausibly be called, from the declaration alone.

    The encoding string is doing the work here. `PUMS RAC1P, recoded detailed race`
    contains the codebook name a raw extract will actually use, so the aliases come
    out of the declaration rather than a hand written synonym list that would rot.
    """
    words = {attribute, attribute.replace("_", " ")}
    encoding = spec.get("encoding") or ""
    # codebook tokens: upper case runs like RAC1P, DIS, NATIVITY, AGEP
    words |= {t.lower() for t in re.findall(r"\b[A-Z][A-Z0-9]{1,9}\b", encoding)
              if t not in {"PUMS"}}
    # trailing prose in the encoding, e.g. "recoded detailed race"
    words |= {w for w in _norm(encoding).split() if len(w) > 3 and w != "pums"}
    return {w for w in words if w}


# Suffixes a warehouse adds to say how a value is stored rather than what it means.
# `place_of_birth_code` is the same column as `place_of_birth`, and without
# stripping these an attribute whose name is more than one word never matches
# exactly, which downgrades a certain answer to an ambiguous one.
# Only storage words. `status` and `type` are deliberately absent: stripping them
# would turn marital_status into marital and break a match that already worked.
NOISE_TOKENS = {"code", "cd", "flag", "id", "key", "num", "usd", "amt", "ind"}


def _strip_noise(name: str) -> str:
    tokens = _norm(name).split()
    while len(tokens) > 1 and tokens[-1] in NOISE_TOKENS:
        tokens.pop()
    return " ".join(tokens)


def by_name(column: str, declared: dict) -> list[dict]:
    """Rung 1. Does the column name say what it is?"""
    target = _strip_noise(column)
    tokens = set(target.split())
    hits = []
    for attribute, spec in declared.items():
        aliases = name_aliases(attribute, spec)
        exact = [a for a in aliases if a in tokens or _norm(a) == target]
        loose = [a for a in aliases
                 if a not in exact and len(a) > 3 and a in target]
        if exact:
            hits.append({"attribute": attribute, "strength": "exact", "on": sorted(exact)})
        elif loose:
            hits.append({"attribute": attribute, "strength": "loose", "on": sorted(loose)})
    return hits


def profile(conn, table: str, column: str) -> dict | None:
    """The distinct values of a column and how common each is."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?", table):
        raise SystemExit(f"refusing to query {table!r}")
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", column):
        raise SystemExit(f"refusing to query column {column!r}")
    with conn.cursor() as cur:
        cur.execute(
            f'select "{column}", count(*) from {table} '
            f'where "{column}" is not null group by 1 order by 2 desc limit {MAX_CODES + 1}'
        )
        rows = cur.fetchall()
    if not rows or len(rows) > MAX_CODES:
        return None
    total = sum(int(n) for _, n in rows)
    if not total:
        return None
    return {
        "distinct": len(rows),
        "total": total,
        "share": {v: int(n) / total for v, n in rows},
    }


def by_values(prof: dict | None, declared: dict) -> list[dict]:
    """Rung 2. Do the values look like a declared coding?

    Matches on the code set only. Two attributes coded over the same small set of
    numbers are genuinely indistinguishable this way, and saying so is the honest
    answer rather than picking the first one.
    """
    if not prof:
        return []
    seen = set(prof["share"])
    hits = []
    for attribute, spec in declared.items():
        codes = set((spec.get("categories") or {}))
        if not codes:
            continue
        # values the coding does not define, weighted by how much of the column
        # they account for. A stray reserved code is tolerable, a different coding
        # is not.
        stray = sum(share for value, share in prof["share"].items() if value not in codes)
        covered = len(seen & codes) / len(codes)
        if stray <= TOLERANCE and covered >= 0.5:
            hits.append({
                "attribute": attribute,
                "codes_declared": len(codes),
                "codes_present": len(seen & codes),
                "outside": round(stray, 4),
            })
    return hits


def verdict(name_hits: list[dict], value_hits: list[dict]) -> dict:
    """Combine the two cheap rungs into one answer, and say when to escalate."""
    named = {h["attribute"] for h in name_hits if h["strength"] == "exact"}
    loose = {h["attribute"] for h in name_hits if h["strength"] == "loose"}
    valued = {h["attribute"] for h in value_hits}

    agreed = named & valued
    if len(agreed) == 1:
        return {"call": "identified", "attribute": next(iter(agreed)),
                "why": "the name and the values agree", "escalate": False}
    if len(named) == 1 and not valued:
        return {"call": "identified", "attribute": next(iter(named)),
                "why": "the name says so and the values could not be profiled",
                "escalate": False}
    if len(valued) == 1 and not named:
        return {"call": "identified", "attribute": next(iter(valued)),
                "why": "the values match this coding and nothing else, with no help "
                       "from the name",
                "escalate": False}
    if len(valued) > 1:
        narrowed = valued & (named | loose)
        if len(narrowed) == 1:
            return {"call": "identified", "attribute": next(iter(narrowed)),
                    "why": f"the values fit {len(valued)} codings and the name "
                           f"chose between them",
                    "escalate": False}
        return {"call": "ambiguous", "candidates": sorted(valued),
                "why": "several attributes share this code set and the name does "
                       "not choose between them",
                "escalate": True}
    if loose:
        return {"call": "ambiguous", "candidates": sorted(loose),
                "why": "the name hints but the values do not confirm it",
                "escalate": True}
    return {"call": "unknown",
            "why": "neither the name nor the values match a declared attribute",
            "escalate": True}


def scan(table: str) -> list[dict]:
    # deliberately the raw attribute declarations rather than a regime. Which
    # column holds disability is a fact about the data and is the same under every
    # statute, so a regime here would only narrow the search for no reason.
    declared = attributes()
    schema, _, bare = table.partition(".")
    out = []
    with psycopg2.connect(WAREHOUSE) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select column_name from information_schema.columns "
                "where table_schema = %s and table_name = %s order by ordinal_position",
                (schema, bare),
            )
            columns = [r[0] for r in cur.fetchall()]
        if not columns:
            raise SystemExit(f"no such table: {table}")
        for column in columns:
            prof = profile(conn, table, column)
            name_hits = by_name(column, declared)
            value_hits = by_values(prof, declared)
            row = {"column": column, "name_hits": name_hits,
                   "value_hits": value_hits, "distinct": (prof or {}).get("distinct")}
            row.update(verdict(name_hits, value_hits))
            out.append(row)
    return out


def _report(table: str, rows: list[dict]) -> None:
    print(f"== what does {table} hold ==")
    print("   no tags read, no lineage walked. names and values only.\n")
    found = [r for r in rows if r["call"] == "identified"]
    unsure = [r for r in rows if r["call"] == "ambiguous"]

    for r in found:
        rungs = []
        if any(h["strength"] == "exact" for h in r["name_hits"]):
            rungs.append("name")
        if r["value_hits"]:
            rungs.append("values")
        print(f"  {'identified':12} {r['column']:34} {r['attribute']}")
        print(f"  {'':12} {' and '.join(rungs)}: {r['why']}")

    if unsure:
        print()
        for r in unsure:
            print(f"  {'ambiguous':12} {r['column']:34} "
                  f"{', '.join(r.get('candidates') or [])}")
            print(f"  {'':12} {r['why']}")
            print(f"  {'':12} escalate to reconstruct.py, which does not need the name")

    quiet = len(rows) - len(found) - len(unsure)
    print(f"\n  {len(found)} identified, {len(unsure)} ambiguous, "
          f"{quiet} with nothing to say")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("table", help="schema qualified, e.g. public.raw_person")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = scan(args.table)
    if args.json:
        print(json.dumps({"table": args.table, "columns": rows}, indent=2))
        return 0
    _report(args.table, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
