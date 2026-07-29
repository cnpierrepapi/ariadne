"""File Ariadne's findings back into DataHub as incidents.

A finding that lives in a terminal is a finding nobody acts on. The catalog is
already where a data team looks when something is wrong with a table, so that is
where the finding belongs, next to the schema and the lineage that explain it.

Two kinds of finding are filed, and they are established by different means:

  the tag check      a restricted attribute reaches a deployed model, found by
                     walking column lineage and reading governance tags. A fact
                     about the graph, no statistics involved.
  the delta watch    what the model can rebuild about people moved further than
                     the measured noise allows. Found statistically, and needs no
                     tag anywhere.

WHERE THE INCIDENT GOES, AND WHY IT IS NOT THE MODEL. DataHub v1.5 does not
accept an incident on an mlModel urn. The mutation is rejected at the entity
layer rather than quietly ignored: only dataset, dataJob, dataFlow, dashboard and
chart carry the aspect. So the incident is filed on the FEATURE TABLE.

That is the better answer rather than a consolation. The table is where the
column entered, where the change was made and where a fix would go. The model is
what the change happened to. An incident on the table reaches the people who can
act on it, and the model, its version and the offending column are named in the
body so the thread is not lost.

It is filed on the dbt entity, which is the primary sibling of the three that
share the table name, so the incident appears on the table's page in the UI
rather than on one of its shadows.

    python tools/incident.py --model workforce-classifier --policy employment_us
    python tools/incident.py --model workforce-classifier --policy employment_us --raise

Nothing is written without `--raise`. The default prints exactly what would be
filed. A tool that writes into a shared catalog as a side effect of being run is
worse than one that writes nothing.
"""

from __future__ import annotations

import argparse
import json
import time

import graph
import sentinel
import trace
from exposure import compare, for_model, load
from policy import load as load_policy

# Neither finding is a freshness, volume, schema or SQL problem, and calling one
# of them FIELD to avoid the word CUSTOM would be dressing it up as something
# DataHub already understands. CUSTOM with an explicit customType says what it is,
# and the customType is the invariant's own name.
CUSTOM_TYPE = {
    "tag": "restricted attribute reaches a deployed model",
    "delta": "reconstructability of a protected attribute moved",
}

# how seriously the regime treats the basis, mapped onto DataHub's four levels.
# `prohibited` means the regime allows no exception, so it is not a question for
# a person. `examine` is a duty to look and document, which is real but not a fault.
PRIORITY = {"prohibited": "CRITICAL", "conditional": "HIGH", "examine": "LOW"}

# the three sibling entities share a table name. dbt is the primary one, so an
# incident filed there lands on the page a person actually opens.
PLATFORM_PREFERENCE = ("dbt", "postgres", "mlflow")

# the catalog the urns are built from, needed because MLflow logs a table name
# without it. Matches CATALOG_PREFIX in ml/train.py.
CATALOG = "warehouse"

_RAISE = "mutation($input: RaiseIncidentInput!) { raiseIncident(input: $input) }"

_EXISTING = """
query($urn: String!) {
  dataset(urn: $urn) {
    incidents(start: 0, count: 200) {
      total
      incidents { urn title status { state } }
    }
  }
}
"""


def resource_for(table: str) -> str | None:
    """The entity to file against, preferring the primary sibling.

    The two sources of a table name disagree about the catalog. The graph names a
    dataset `warehouse.analytics_marts.workforce_features`, because the urn is
    built from the fully qualified name, while the MLflow run logs the table the
    way the training query wrote it, without the database. Both are the same
    table, so try the name as given and then with the catalog in front. Without
    this the delta finding silently has nowhere to go.
    """
    for name in (table, f"{CATALOG}.{table}"):
        found = trace.entities(name)
        if not found:
            continue
        for platform in PLATFORM_PREFERENCE:
            for urn in found:
                if trace.platform(urn) == platform:
                    return urn
        return found[0]
    return None


def open_titles(resource_urn: str) -> set[str]:
    """Titles of incidents already active on the resource, so reruns stay quiet.

    Deduplicating on title rather than on the body is deliberate. The body carries
    measured numbers that move slightly on every refit, so comparing bodies would
    file a near duplicate every time the check ran, and a catalog full of those is
    indistinguishable from a broken alarm.
    """
    data = graph.gql(_EXISTING, {"urn": resource_urn})
    found = ((data.get("dataset") or {}).get("incidents") or {}).get("incidents") or []
    return {
        i["title"] for i in found
        if ((i.get("status") or {}).get("state")) == "ACTIVE" and i.get("title")
    }


def _belongs_to(model_name: str, found_name: str) -> bool:
    """mlflow names a model version `name_3`, so match the registered name."""
    return found_name == model_name or found_name.rsplit("_", 1)[0] == model_name


def tag_findings(model_name: str, regime: str | None) -> list[dict]:
    findings, _warnings = sentinel.violations(regime)
    return [
        {
            "kind": "tag",
            "table": f["table"],
            "model": f["model_name"],
            "column": f["feature"],
            "basis": f["basis"],
            "attribute": f["attribute"] or "",
            "citation": f["citation"] or "",
            "hops": f["hops"],
            "origin_table": f["origin_table"],
            "origin_column": f["origin_column"],
        }
        for f in findings if _belongs_to(model_name, f["model_name"])
    ]


def _feature_table(current: dict, model_name: str) -> str | None:
    """The table the recording was made against.

    Recordings made before the feature table was stored in the history carry no
    table at all, and there are such recordings in this repo's own evidence. Ask
    the registry what the serving version trained on rather than dropping the
    finding, because a finding that vanishes for a bookkeeping reason is the worst
    kind to lose.
    """
    stored = current.get("feature_table")
    if stored:
        return stored
    try:
        from reconstruct import deployed

        return deployed(model_name).get("feature_table")
    except (SystemExit, ImportError):
        return None


def delta_findings(model_name: str) -> tuple[list[dict], dict, dict]:
    """Findings from the two most recent recordings of one model."""
    recordings = for_model(load(), model_name)
    if len(recordings) < 2:
        return [], {}, {}
    previous, current = recordings[-2], recordings[-1]
    findings, context = compare(previous, current)
    table = _feature_table(current, model_name)
    for f in findings:
        f["kind"] = "delta"
        f["table"] = table
    return findings, context, current


def _title(f: dict, pack) -> str:
    """The title, which is also the deduplication key, so it must name the regime.

    A column reaching a model is one fact about the graph, but it is a different
    obligation under each statute, at a different severity, with a different
    citation. `age` is prohibited under the Canadian Human Rights Act and merely
    conditional under ECOA. Filing one incident for both would mean whichever
    regime was checked first silently decided the severity for a jurisdiction
    nobody had looked at yet.

    So the regime belongs in the title. Without it the second regime to run
    deduplicates against the first and reports `already open` while having filed
    nothing, which is the exact shape of failure this project keeps hitting: a
    check that looks like it worked.
    """
    if f["kind"] == "tag":
        base = f"{f['column']} reaches {f['model']}"
    else:
        # the group belongs in the title. Each group is measured against the
        # reference group separately, the way adverse impact analysis compares, so
        # one attribute can produce several findings at once. Without the group
        # they share a title, and deduplication then drops all but the first,
        # which loses real findings while looking like it worked.
        base = (f"{f['attribute']} ({f['group']}) became easier to rebuild from "
                f"{f['model']}")
    return f"{base} under {pack.name}"


def _body_tag(f: dict, pack) -> str:
    if f["hops"]:
        plural = "hop" if f["hops"] == 1 else "hops"
        where = (f"It is untagged on the feature table itself. The tag is on "
                 f"{f['origin_table']}.{f['origin_column']}, {f['hops']} {plural} "
                 f"back, and was found by following the column there.")
    else:
        where = "It is tagged on the feature table itself."
    lines = [
        f"{f['column']} reaches {f['model']}, which the registry has in the "
        f"Production stage.",
        "",
        f"The column is in {f['table']}, the table the model trained on. {where}",
        "",
        f"Regime: {pack.long_name}",
        f"Basis: {f['basis']}"
        + (f", as {f['attribute']}" if f["attribute"] else ", not a declared attribute"),
    ]
    if f["citation"]:
        lines.append(f"Citation: {f['citation']}")
    lines += [
        "",
        "Established by walking column level lineage from the feature back through "
        "the warehouse and reading the governance tags on every entity carrying "
        "that column. This is a fact about the graph, not a measurement.",
    ]
    return "\n".join(lines)


def _body_delta(f: dict, context: dict) -> str:
    gained = context.get("features_gained") or []
    was, now = context.get("accuracy_was"), context.get("accuracy_now")
    lines = [
        f"How much of {f['attribute']} the deployed model can rebuild from the "
        f"features it does contain has moved.",
        "",
        f"{f['group']} against {f['against']}: {f['was']:.4f} to {f['now']:.4f}, "
        f"a change of {f['delta']:+.4f}, which is {f['multiples_of_noise']:.0f} "
        f"times the measured noise of {f['noise_stdev']:.4f}.",
        f"Between v{f['from_version']} and v{f['to_version']} of {f['model']}.",
        "",
    ]
    if gained:
        lines += [
            f"The deployed model gained {', '.join(gained)} between the two "
            f"recordings, which is where to look first.",
            "",
        ]
    if was is not None and now is not None:
        direction = "up" if now > was else "down"
        lines += [
            f"Model accuracy went {was:.4f} to {now:.4f} over the same change, "
            f"which is {direction}. The metric a team watches did not raise this "
            f"and could not have.",
            "",
        ]
    lines += [
        "The attribute under test is excluded from its own inputs, so this is not "
        "the model reading the answer back to itself. What counts as a move is the "
        "larger of four times the spread measured across refits and a floor of "
        "0.02, so anything smaller is reported as the same number measured twice.",
        "",
        "This finding needs no tag anywhere. It is what the model knows, not what "
        "the warehouse admits to holding.",
    ]
    return "\n".join(lines)


def to_incident(f: dict, pack, context: dict, resource_urn: str) -> dict:
    tag_kind = f["kind"] == "tag"
    return {
        "type": "CUSTOM",
        "customType": CUSTOM_TYPE[f["kind"]],
        "title": _title(f, pack),
        "description": _body_tag(f, pack) if tag_kind else _body_delta(f, context),
        "resourceUrn": resource_urn,
        "priority": PRIORITY.get(f["basis"], "MEDIUM") if tag_kind else "HIGH",
        "source": {"type": "MANUAL"},
        "status": {"state": "ACTIVE", "stage": "TRIAGE"},
        "startedAt": int(time.time() * 1000),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True)
    ap.add_argument("--policy", help="regime, see tools/policy.py list")
    ap.add_argument("--raise", dest="do_raise", action="store_true",
                    help="actually write into DataHub")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    pack = load_policy(args.policy)
    everything = tag_findings(args.model, args.policy)
    deltas, context, _current = delta_findings(args.model)
    everything += deltas

    if not everything:
        print(f"nothing to file for {args.model}")
        return 0

    planned: list[dict] = []
    for f in everything:
        resource = resource_for(f["table"]) if f.get("table") else None
        if not resource:
            print(f"  skipped {_title(f, pack)}: no entity for {f.get('table')}")
            continue
        planned.append(to_incident(f, pack, context, resource))

    if args.json:
        print(json.dumps(planned, indent=2))
        return 0

    print(f"== {len(planned)} finding(s) for {args.model} under {pack.long_name} ==\n")
    already: dict[str, set[str]] = {}
    for incident in planned:
        resource = incident["resourceUrn"]
        if resource not in already:
            already[resource] = open_titles(resource)
        skip = incident["title"] in already[resource]
        mark = "already open" if skip else ("filing" if args.do_raise else "would file")
        print(f"  [{mark}] {incident['priority']:8} {incident['title']}")
        print(f"      on {resource}")
        print(f"      {incident['customType']}")
        if skip or not args.do_raise:
            continue
        result = graph.gql(_RAISE, {"input": incident})
        already[resource].add(incident["title"])
        print(f"      raised {result.get('raiseIncident')}")

    if not args.do_raise:
        print("\n  nothing was written. pass --raise to file these.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
