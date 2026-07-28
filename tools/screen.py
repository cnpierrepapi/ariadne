"""Measure the declared proxy hypotheses, and let most of them fail.

The register in research/proxies.yml says what each candidate column is
supposed to carry and where that claim comes from. This measures whether it
does, on this warehouse, with the same machinery the exposure checks use.

Two numbers per hypothesis, because one is not enough to act on:

  alone  how well the column by itself rebuilds the attribute. Answers "is
         there anything in here at all".
  lift   how much it adds to a model that already has the ordinary features.
         Answers the question that matters, which is whether adding this column
         to a feature table would put more of the attribute into a model than
         is already there. A column can score well alone and add nothing,
         because the features already present carry the same information.

Lift is the one that decides. It is also the one that is easy to get wrong by
comparing against a baseline refitted on a different split, so the baseline and
every candidate are scored on the same seeds and the same rows.

What counts as a real lift is the threshold from history.py, the same one the
exposure check fires on. Using a looser threshold here would produce a register
full of findings that the check downstream would then ignore.

    python tools/screen.py
    python tools/screen.py --model workforce-classifier --repeats 3
    python tools/screen.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timezone

import yaml

from history import FLOOR, SIGMAS
from reconstruct import _person_columns, _prepare, _score, deployed, read_frame

REGISTER = os.environ.get(
    "ARIADNE_PROXY_REGISTER",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "research", "proxies.yml"),
)
RESULTS = os.environ.get(
    "ARIADNE_SCREEN_RESULTS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "state", "screen.json"),
)


def register() -> dict:
    with open(REGISTER, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _auc(frame, columns: list[str], y, seeds: tuple[int, ...]) -> tuple[float, float]:
    runs = [_score(_prepare(frame, columns), y, seed) for seed in seeds]
    aucs = [r["auc"] for r in runs]
    spread = statistics.stdev(aucs) if len(aucs) > 1 else 0.0
    return sum(aucs) / len(aucs), spread


def screen(model_name: str, seeds: tuple[int, ...]) -> dict:
    spec = register()
    target = spec["target_column"]
    serving = deployed(model_name)

    # the baseline is the deployed model's own features, minus the attribute and
    # minus anything the register is about to propose, so a candidate is never
    # compared against a baseline that already contains it
    proposed = {h["column"] for h in spec["hypotheses"] if h.get("column")}
    baseline_columns = [c for c in serving["features"]
                        if c != target and c not in proposed]

    # Candidates are read from the person dimension, not the feature table. A
    # proxy that is already in the feature table is not a hypothesis any more,
    # and the whole question is what would happen if one were added, so they
    # have to come from where they still live.
    person = _person_columns()
    available = sorted(c for c in proposed if c in person)
    frame = read_frame(baseline_columns, [target] + available,
                       serving["feature_table"])
    frame = frame.dropna(subset=[target])
    y = (frame[target] == 1).astype(int)

    base_auc, base_spread = _auc(frame, baseline_columns, y, seeds)

    results = []
    for h in spec["hypotheses"]:
        column = h.get("column")
        row = {"id": h["id"], "pums": h.get("pums"), "column": column,
               "strength": h.get("strength"),
               "sourced": bool(h.get("source"))}
        if not column:
            row.update({"tested": False,
                        "why_not": "the concept is not a column in this warehouse"})
            results.append(row)
            continue
        if column not in frame.columns:
            row.update({"tested": False,
                        "why_not": f"{column} is declared in the register but "
                                   f"is not a column of the person dimension"})
            results.append(row)
            continue

        alone, _ = _auc(frame, [column], y, seeds)
        with_it, spread = _auc(frame, baseline_columns + [column], y, seeds)
        lift = with_it - base_auc
        pooled = max((base_spread + spread) / 2, 0.001)
        threshold = max(FLOOR, SIGMAS * pooled)
        row.update({
            "tested": True, "alone": alone, "with_baseline": with_it,
            "lift": lift, "noise": pooled, "threshold": threshold,
            "fires": lift >= threshold,
        })
        results.append(row)

    results.sort(key=lambda r: (not r.get("tested"), -(r.get("lift") or -9)))
    return {
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "attribute": spec["attribute"], "target_column": target,
        "model": serving["model"], "version": serving["version"],
        "feature_table": serving["feature_table"],
        "baseline_features": baseline_columns,
        "baseline_auc": base_auc, "baseline_noise": base_spread,
        "rows": int(len(frame)), "seeds": list(seeds),
        "results": results,
    }


def _report(out: dict) -> None:
    print(f"== can any declared proxy rebuild {out['attribute']} ==")
    print(f"   {out['rows']} rows, {len(out['baseline_features'])} baseline "
          f"features from {out['model']} v{out['version']}")
    print(f"   baseline rebuilds {out['attribute']} at {out['baseline_auc']:.4f} "
          f"before any candidate is added")
    print(f"   seeds {', '.join(str(s) for s in out['seeds'])}\n")

    fired = [r for r in out["results"] if r.get("fires")]
    quiet = [r for r in out["results"] if r.get("tested") and not r["fires"]]
    skipped = [r for r in out["results"] if not r.get("tested")]

    print(f"  {'candidate':36} {'alone':>7} {'lift':>8}   verdict")
    for r in fired + quiet:
        verdict = "fires" if r["fires"] else "silent"
        source = "" if r["sourced"] else "  (no source, reasoned from the concept)"
        print(f"  {r['pums'] + ' ' + r['column']:36} {r['alone']:7.4f} "
              f"{r['lift']:+8.4f}   {verdict}{source}")

    for r in skipped:
        print(f"  {(r['pums'] or r['id']):36} {'':7} {'':8}   not tested, "
              f"{r['why_not']}")

    print(f"\n  {len(fired)} of {len(fired) + len(quiet)} tested hypotheses "
          f"moved the number by more than {out['results'][0]['threshold']:.4f}, "
          f"which is the same threshold the exposure check fires on")
    if quiet:
        print(f"  {len(quiet)} did not, and stay in the register as refuted "
              f"rather than being deleted")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="workforce-classifier")
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--repeats", type=int, default=3,
                    help="seeds to average over, so the noise is measured "
                         "rather than assumed")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    seeds = tuple(args.seed + i for i in range(max(1, args.repeats)))
    out = screen(args.model, seeds)

    if not args.no_write:
        os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
        with open(RESULTS, "w", encoding="utf-8") as handle:
            json.dump(out, handle, indent=2)
            handle.write("\n")

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        _report(out)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, __file__.rsplit("/", 1)[0].rsplit("\\", 1)[0])
    raise SystemExit(main())
