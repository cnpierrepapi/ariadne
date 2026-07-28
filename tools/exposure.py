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

A recording belongs to one model. The history file holds every model's, so a check
compares a model against its own last recording and never against whatever happened
to be written most recently. Two models watched at once would otherwise be compared
to each other, and the answer would be arithmetic on unrelated numbers.

    python tools/exposure.py record --model income-classifier
    python tools/exposure.py check --model income-classifier
    python tools/exposure.py history
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from reconstruct import deployed, reconstruct, seeds_from

# Reading the history and deciding what counts as a change live in history.py,
# which imports nothing heavy, so the agents can ask the same question without
# dragging in scikit-learn. Re-exported here because this is still the command
# people run and the names were public before the split.
from history import (  # noqa: F401
    FLOOR,
    HISTORY,
    SIGMAS,
    _key,
    compare,
    for_model,
    load,
    models_recorded,
    save,
)


def record(model_name: str, seed: int, repeats: int,
           regime: str | None = None) -> dict:
    serving = deployed(model_name)
    measurements = reconstruct(serving["features"], seeds_from(seed, repeats),
                               regime, serving["feature_table"])
    entry = {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": serving["model"],
        "version": serving["version"],
        "run_id": serving["run_id"],
        "feature_table": serving["feature_table"],
        "regime": regime,
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
    p_record.add_argument("--policy", help="regime to read the sensitive attributes from")

    p_check = sub.add_parser("check", help="compare a model's last two recordings")
    p_check.add_argument("--model", default="income-classifier")
    p_check.add_argument("--json", action="store_true")
    p_check.add_argument("--fail-on-violation", action="store_true")

    sub.add_parser("history", help="what has been recorded")

    args = ap.parse_args()

    if args.cmd == "record":
        entry = record(args.model, args.seed, args.repeats, args.policy)
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
        for model in models_recorded(recordings):
            print(model)
            for entry in for_model(recordings, model):
                print(f"  {entry['recorded_at']}  v{entry['version']}  "
                      f"{len(entry['features'])} features")
        return 0

    mine = for_model(recordings, args.model)
    if len(mine) < 2:
        print(f"only {len(mine)} recording of {args.model} so far, so there is "
              f"nothing to compare against yet. run record again after the pipeline "
              f"changes.")
        if recordings and not mine:
            print(f"recorded models: {', '.join(models_recorded(recordings))}")
        return 0

    previous, current = mine[-2], mine[-1]
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
