"""Watch how much of a protected attribute a model carries, and fire when it moves.

The measurement in reconstruct.py produces a number that is hard to act on by
itself. Race is recoverable from the deployed model's features at 0.73. Is that
bad? Compared to what? Education correlates with race because society does, and no
threshold on the level can separate that from a fault in the pipeline without
somebody arguing about it.

The change is a different matter. If the number sits at 0.73 for a year and then
reaches 0.82 the week somebody edited a dbt model, that is not a fact about
society. Something entered the pipeline. That is worth an alarm, and it needs no
argument about what an acceptable level would be.

Ariadne is unusually well placed to watch it, because it can see both the number
and the change that moved it. So a finding here does not only say the number rose.
It says which columns the deployed model gained since the last recording, which is
almost always the answer.

What counts as moved is measured rather than chosen. Refitting on a different split
moves the number by a standard deviation of about 0.003 on this data, so the
default threshold is the larger of four times the observed spread and a floor of
0.02, which is roughly seven times that noise. Anything under it is the same
number measured twice.

    python tools/exposure.py record
    python tools/exposure.py check
    python tools/exposure.py history
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from reconstruct import deployed, reconstruct, seeds_from

HISTORY = os.environ.get(
    "ARIADNE_EXPOSURE_HISTORY",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "state", "exposure.json"),
)

# below this, two recordings are the same number measured twice
FLOOR = 0.02
# multiples of the observed spread that count as a real move
SIGMAS = 4.0


def load() -> list[dict]:
    if not os.path.exists(HISTORY):
        return []
    with open(HISTORY, encoding="utf-8") as handle:
        return json.load(handle).get("recordings", [])


def save(recordings: list[dict]) -> None:
    os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
    with open(HISTORY, "w", encoding="utf-8") as handle:
        json.dump({"recordings": recordings}, handle, indent=2)
        handle.write("\n")


def record(model_name: str, seed: int, repeats: int) -> dict:
    serving = deployed(model_name)
    measurements = reconstruct(serving["features"], seeds_from(seed, repeats))
    entry = {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": serving["model"],
        "version": serving["version"],
        "run_id": serving["run_id"],
        "model_accuracy": serving["accuracy"],
        "features": serving["features"],
        "measurements": [
            {k: m[k] for k in ("attribute", "column", "group", "against",
                               "auc", "auc_stdev", "seeds", "rows")}
            for m in measurements
        ],
    }
    recordings = load()
    recordings.append(entry)
    save(recordings)
    return entry


def _key(measurement: dict) -> tuple[str, str]:
    return measurement["column"], measurement["group"]


def compare(previous: dict, current: dict) -> tuple[list[dict], dict]:
    """Findings where the number moved further than the noise allows."""
    before = {_key(m): m for m in previous["measurements"]}
    findings: list[dict] = []

    for now in current["measurements"]:
        was = before.get(_key(now))
        if not was:
            continue
        delta = now["auc"] - was["auc"]
        # pooled spread of the two recordings, floored so a single seed recording
        # does not produce a threshold of zero and fire on nothing
        spread = max((was["auc_stdev"] + now["auc_stdev"]) / 2, 0.001)
        threshold = max(FLOOR, SIGMAS * spread)
        if abs(delta) < threshold:
            continue
        findings.append({
            "invariant": "reconstructability of a protected attribute moved",
            "model": current["model"],
            "attribute": now["attribute"],
            "group": now["group"],
            "against": now["against"],
            "was": was["auc"],
            "now": now["auc"],
            "delta": delta,
            "threshold": threshold,
            "noise_stdev": spread,
            "multiples_of_noise": abs(delta) / spread,
            "from_version": previous["version"],
            "to_version": current["version"],
        })

    gained = [f for f in current["features"] if f not in previous["features"]]
    lost = [f for f in previous["features"] if f not in current["features"]]
    context = {
        "features_gained": gained,
        "features_lost": lost,
        "accuracy_was": previous.get("model_accuracy"),
        "accuracy_now": current.get("model_accuracy"),
    }
    return findings, context


def _report(findings: list[dict], context: dict, previous: dict, current: dict) -> None:
    print("== reconstructability of a protected attribute moved ==")
    print(f"   {previous['model']} v{previous['version']} recorded "
          f"{previous['recorded_at']}")
    print(f"   {current['model']} v{current['version']} recorded "
          f"{current['recorded_at']}")

    gained, lost = context["features_gained"], context["features_lost"]
    if gained:
        print(f"   features gained: {', '.join(gained)}")
    if lost:
        print(f"   features lost: {', '.join(lost)}")
    was, now = context["accuracy_was"], context["accuracy_now"]
    if was is not None and now is not None:
        print(f"   model accuracy {was:.4f} to {now:.4f} ({now - was:+.4f})")

    if not findings:
        print("\n  nothing moved further than the noise allows")
        return

    print()
    for f in sorted(findings, key=lambda f: abs(f["delta"]), reverse=True):
        print(f"  {f['attribute']}: {f['group']} against {f['against']}")
        print(f"    {f['was']:.4f} to {f['now']:.4f}  ({f['delta']:+.4f}, "
              f"{f['multiples_of_noise']:.0f} times the measured noise of "
              f"{f['noise_stdev']:.4f})")
    if gained:
        print(f"\n  the deployed model gained {', '.join(gained)} between these "
              f"recordings, which is where to look first")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_record = sub.add_parser("record", help="measure the deployed model and store it")
    p_record.add_argument("--model", default="income-classifier")
    p_record.add_argument("--seed", type=int, default=17)
    p_record.add_argument("--repeats", type=int, default=3,
                          help="splits per measurement, which is what sizes the noise")

    p_check = sub.add_parser("check", help="compare the last two recordings")
    p_check.add_argument("--json", action="store_true")
    p_check.add_argument("--fail-on-violation", action="store_true")

    sub.add_parser("history", help="what has been recorded")

    args = ap.parse_args()

    if args.cmd == "record":
        entry = record(args.model, args.seed, args.repeats)
        print(f"recorded {entry['model']} v{entry['version']} at {entry['recorded_at']}")
        for m in sorted(entry["measurements"], key=lambda m: m["auc"], reverse=True):
            print(f"  {m['auc']:.4f} plus or minus {m['auc_stdev']:.4f}  "
                  f"{m['attribute']}: {m['group']} against {m['against']}")
        print(f"stored in {HISTORY}")
        return 0

    recordings = load()

    if args.cmd == "history":
        if not recordings:
            print(f"nothing recorded yet in {HISTORY}")
            return 0
        for entry in recordings:
            print(f"{entry['recorded_at']}  {entry['model']} v{entry['version']}  "
                  f"{len(entry['features'])} features")
        return 0

    if len(recordings) < 2:
        print(f"only {len(recordings)} recording so far, so there is nothing to "
              f"compare against yet. run record again after the pipeline changes.")
        return 0

    previous, current = recordings[-2], recordings[-1]
    findings, context = compare(previous, current)

    if args.json:
        print(json.dumps({"findings": findings, "context": context,
                          "from": previous["recorded_at"],
                          "to": current["recorded_at"]}, indent=2))
    else:
        _report(findings, context, previous, current)

    return 1 if (findings and args.fail_on_violation) else 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(main())
