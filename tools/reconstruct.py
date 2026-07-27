"""Ask whether a model's features can rebuild an attribute the model does not contain.

Removing a protected column from a feature table is the control most teams
believe they have applied. It is only a real control if the remaining columns do
not carry the same information. Income, education, occupation and neighbourhood
are each correlated with race, and enough of them together can reconstruct it.
When that happens the column was removed and the information was not, and the
model reasons from a prohibited basis while nothing in the pipeline says so.

This measures that directly rather than arguing about it. Drop the attribute from
the feature list, fit an ordinary model to predict it from everything that
remains, and score on held out rows. The output is a number, which is the point:
it can be shown to somebody who has to sign off, and it is checkable.

Two things this deliberately does not do.

It does not decide legality. Correlation with race is not prohibited and cannot
be, because income and education correlate with race and a model stripped of
everything correlated with race predicts nothing at all. What the law asks is
whether a practice causes harm, whether the business need justifies it, and
whether a less harmful alternative exists. Those are human decisions. This
supplies one input to them.

It does not need the attribute to be labelled anywhere. That is what separates it
from the tag driven checks in sentinel.py. It needs the attribute to exist in the
warehouse, which is usually true, because collecting demographics for statutory
reporting is often required even where using them in decisions is forbidden.

    python tools/reconstruct.py --model income-classifier
    python tools/reconstruct.py --model income-classifier --json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import warnings

import pandas as pd
import psycopg2
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.model_selection import train_test_split

from policy import load as load_policy

warnings.filterwarnings("ignore")

WAREHOUSE = os.environ.get(
    "ARIADNE_WAREHOUSE_URL", "postgresql://ariadne:ariadne@localhost:5433/warehouse"
)
TRACKING = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")

FEATURE_TABLE = "analytics_marts.income_features"
PERSON_TABLE = "analytics_marts.dim_person"
JOIN_KEY = "person_id"

def sensitive(regime: str | None = None) -> dict[str, dict]:
    """Attributes to attempt to rebuild, keyed by the column holding each.

    Comes from the declared regime rather than a list in this file, so pointing the
    measurement at a hiring model under the ADA is a matter of naming a different
    policy pack. Each is tested one group against the reference group, because fair
    lending and adverse impact analysis both compare that way rather than lumping
    everyone who is not in the reference group together.
    """
    return {
        spec["column"]: {
            "label": spec["attribute"],
            "groups": spec["categories"],
            "reference": spec["reference"],
            "basis": spec["basis"],
            "citation": spec.get("citation", ""),
        }
        for spec in load_policy(regime).testable()
    }


def deployed(model_name: str) -> dict:
    """Whichever version the registry says is serving, and what it trained on."""
    from mlflow.tracking import MlflowClient

    client = MlflowClient(TRACKING)
    serving = [
        mv for mv in client.search_model_versions(f"name='{model_name}'")
        if mv.current_stage == "Production"
    ]
    if not serving:
        raise SystemExit(f"no version of {model_name} is in the Production stage")
    newest = max(serving, key=lambda mv: int(mv.version))
    run = client.get_run(newest.run_id)
    features = run.data.tags.get("features")
    if not features:
        raise SystemExit(f"run {newest.run_id} did not record its feature list")
    return {
        "model": model_name,
        "version": str(newest.version),
        "run_id": newest.run_id,
        "features": features.split(","),
        "accuracy": run.data.metrics.get("accuracy"),
    }


def deployed_features(model_name: str, quiet: bool = False) -> list[str]:
    """The feature list of whichever version the registry says is serving."""
    serving = deployed(model_name)
    if not quiet:
        print(f"reading the feature list off {model_name} v{serving['version']}, "
              f"the version in Production")
    return serving["features"]


def read_frame(features: list[str], targets: list[str]) -> pd.DataFrame:
    wanted = [c for c in features if c not in targets]
    columns = ", ".join([f"f.{c}" for c in wanted] + [f"d.{t}" for t in targets])
    sql = (f"select {columns} from {FEATURE_TABLE} f "
           f"join {PERSON_TABLE} d using ({JOIN_KEY})")
    with psycopg2.connect(WAREHOUSE) as conn:
        return pd.read_sql(sql, conn)


def _prepare(frame: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    X = frame[features].copy()
    for column in X.columns:
        if X[column].dtype == object:
            X[column] = X[column].astype("category")
    return X


def _score(X: pd.DataFrame, y: pd.Series, seed: int) -> dict:
    """Held out score for rebuilding y from X, against the do nothing baseline."""
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.25, random_state=seed, stratify=y
    )
    model = HistGradientBoostingClassifier(
        max_iter=150, learning_rate=0.1, random_state=seed,
        categorical_features="from_dtype",
    )
    model.fit(X_train, y_train)
    predicted = model.predict(X_test)
    scored = model.predict_proba(X_test)[:, 1]
    # guessing the commonest answer every time, which is what no information looks like
    baseline = max(y_test.mean(), 1 - y_test.mean())
    return {
        "auc": float(roc_auc_score(y_test, scored)),
        "accuracy": float(accuracy_score(y_test, predicted)),
        "baseline_accuracy": float(baseline),
        "positive_rate": float(y_test.mean()),
        "rows": int(len(X)),
    }


def reconstruct(features: list[str], seeds: tuple[int, ...] = (17,),
                regime: str | None = None) -> list[dict]:
    """Score every attribute that can be tested, repeated over seeds.

    Repeats are not decoration. A single split gives a number with no sense of
    its own wobble, and the whole use of this measurement downstream is comparing
    it to an earlier one. Measured spread across seeds is what makes a difference
    between two runs meaningful rather than asserted.
    """
    declared = sensitive(regime)
    targets = [t for t in declared if t in _person_columns()]
    frame = read_frame(features, targets)
    findings: list[dict] = []

    for target in targets:
        spec = declared[target]
        # the attribute under test must not be an input to rebuilding itself. it is
        # a legitimate feature of the deployed model and is read from the person
        # table as the answer, so without this it appears on both sides and scores
        # a perfect one, which is the shape a leak takes rather than a result.
        usable = [c for c in features if c in frame.columns and c != target]
        present = frame[target].dropna().unique()
        for code, name in spec["groups"].items():
            if code == spec["reference"] or code not in present:
                continue
            subset = frame[frame[target].isin([spec["reference"], code])]
            if subset[target].nunique() < 2 or len(subset) < 2000:
                continue
            y = (subset[target] == code).astype(int)
            runs = [_score(_prepare(subset, usable), y, seed) for seed in seeds]
            aucs = [r["auc"] for r in runs]
            findings.append({
                "attribute": spec["label"], "column": target,
                "group": name, "against": spec["groups"][spec["reference"]],
                "basis": spec["basis"], "citation": spec["citation"],
                "features_used": len(usable),
                "auc": sum(aucs) / len(aucs),
                "auc_stdev": (statistics.stdev(aucs) if len(aucs) > 1 else 0.0),
                "seeds": list(seeds),
                "accuracy": runs[0]["accuracy"],
                "baseline_accuracy": runs[0]["baseline_accuracy"],
                "positive_rate": runs[0]["positive_rate"],
                "rows": runs[0]["rows"],
            })
    return findings


def per_feature(features: list[str], target: str, group: int, reference: int,
                seed: int = 17) -> list[dict]:
    """Which single columns carry the attribute, ranked. Where the leak is."""
    frame = read_frame(features, [target])
    subset = frame[frame[target].isin([reference, group])]
    y = (subset[target] == group).astype(int)
    ranked: list[dict] = []
    for column in [c for c in features if c in frame.columns]:
        result = _score(_prepare(subset, [column]), y, seed)
        ranked.append({"feature": column, "auc": result["auc"]})
    return sorted(ranked, key=lambda r: r["auc"], reverse=True)


def seeds_from(seed: int, repeats: int) -> tuple[int, ...]:
    """Deterministic seeds, so a rerun of the same recording gives the same answer."""
    return tuple(seed + offset for offset in range(max(1, repeats)))


def _person_columns() -> set[str]:
    with psycopg2.connect(WAREHOUSE) as conn:
        frame = pd.read_sql(
            "select column_name from information_schema.columns "
            "where table_schema = 'analytics_marts' and table_name = 'dim_person'", conn)
    return set(frame["column_name"])


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="income-classifier")
    ap.add_argument("--features", help="comma separated, instead of reading the registry")
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--repeats", type=int, default=1,
                    help="split and refit this many times, to measure the wobble")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--policy", help="regime to read the sensitive attributes from")
    ap.add_argument("--per-feature", metavar="COLUMN",
                    help="also rank single columns by how much of COLUMN they carry")
    args = ap.parse_args()

    features = (args.features.split(",") if args.features
                else deployed_features(args.model))
    findings = reconstruct(features, seeds_from(args.seed, args.repeats), args.policy)

    ranked = []
    if args.per_feature:
        spec = sensitive(args.policy)[args.per_feature]
        worst = max((f for f in findings if f["column"] == args.per_feature),
                    key=lambda f: f["auc"], default=None)
        if worst:
            code = next(c for c, n in spec["groups"].items() if n == worst["group"])
            ranked = per_feature([f for f in features if f != args.per_feature],
                                 args.per_feature, code, spec["reference"], args.seed)

    if args.json:
        print(json.dumps({"features": features, "findings": findings,
                          "per_feature": ranked}, indent=2))
        return 0

    print(f"\n== rebuilding attributes the model does not contain ==")
    print(f"   {len(features)} features on the deployed model, the attribute under "
          f"test excluded from each run\n")
    for f in sorted(findings, key=lambda f: f["auc"], reverse=True):
        lift = f["accuracy"] - f["baseline_accuracy"]
        print(f"  {f['attribute']}: {f['group']} against {f['against']}")
        spread = f" plus or minus {f['auc_stdev']:.4f}" if f["auc_stdev"] else ""
        print(f"    auc {f['auc']:.3f}{spread}   accuracy {f['accuracy']:.3f} "
              f"against a baseline of {f['baseline_accuracy']:.3f} "
              f"({lift:+.3f})   {f['rows']:,} rows")
    if ranked:
        print(f"\n== which single columns carry it ==")
        for r in ranked:
            print(f"  {r['auc']:.3f}  {r['feature']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
