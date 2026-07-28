"""The recording history, and the one definition of what counts as a change.

Split out of exposure.py rather than copied. Making a recording needs pandas and
scikit-learn, because it refits models to measure reconstructability. Reading one
back needs nothing but the standard library, and the agents only ever read.

Keeping the split honest matters more than the import weight. There must be
exactly one answer to "did this move", because two answers would drift and the
tool would fire in one place and stay silent in the other on the same pair of
numbers. So the threshold lives here and everything that asks the question calls
`compare`.

What counts as moved is measured, not chosen. Refitting on a different split
moves reconstructability by a standard deviation of about 0.003 on this data, so
the threshold is the larger of four times the observed spread and a floor of
0.02, which is roughly seven times that noise.
"""

from __future__ import annotations

import json
import os

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


def for_model(recordings: list[dict], model_name: str | None) -> list[dict]:
    """One model's recordings, in the order they were made.

    A recording belongs to one model. The history file holds every model's, so a
    check compares a model against its own last recording and never against
    whatever happened to be written most recently. Two models watched at once
    would otherwise be compared to each other, and the answer would be
    arithmetic on unrelated numbers.
    """
    if not model_name:
        return recordings
    return [r for r in recordings if r.get("model") == model_name]


def models_recorded(recordings: list[dict]) -> list[str]:
    return sorted({r.get("model", "unknown") for r in recordings})


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
