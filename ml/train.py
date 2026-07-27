"""Train and register a classifier on a warehouse feature table.

Deliberately ordinary. This is what an ML engineer writes on a Tuesday: pull the
feature table, split, fit a gradient boosting model, log the run, register the
version. No tricks, because the point of Ariadne is that nothing about this file
has to change for the lineage to be watchable.

Which table and which label are arguments rather than constants, because the same
script trains the lending shaped model and the employment shaped one. Nothing about
either sector lives here.

    python ml/train.py
    python ml/train.py --feature-table analytics_marts.workforce_features \\
        --label works_full_time --experiment workforce-classifier \\
        --model-name workforce-classifier

The one line that carries weight is `mlflow.log_input`. It records which table the
model was actually trained on, which is what turns a model in a registry into a node
with an upstream. Without it a registered model is an orphan: you know it exists,
you cannot know what made it. Every check Ariadne performs walks through that edge.
"""

from __future__ import annotations

import argparse
import os

import mlflow
import mlflow.sklearn
import pandas as pd
import psycopg2
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.model_selection import train_test_split

from warehouse_source import WarehouseTableSource

WAREHOUSE = os.environ.get(
    "ARIADNE_WAREHOUSE_URL", "postgresql://ariadne:ariadne@localhost:5433/warehouse"
)
TRACKING = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")

FEATURE_TABLE = "analytics_marts.income_features"
LABEL = "income_above_50k"
# columns present in every feature table that are not features: the grain and the
# survey vintage. The label is added to this set once it is known.
NOT_FEATURES = {"person_id", "survey_year"}

# the catalog names a table with its database included, because the urn is built
# from the fully qualified name and the two have to line up
CATALOG_PREFIX = "warehouse"


def read_features(table: str) -> pd.DataFrame:
    if not all(part.replace("_", "").isalnum() for part in table.split(".")):
        raise SystemExit(f"refusing to query {table!r}")
    with psycopg2.connect(WAREHOUSE) as conn:
        return pd.read_sql(f"select * from {table}", conn)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--experiment", default="income-classifier")
    ap.add_argument("--model-name", default="income-classifier")
    ap.add_argument("--feature-table", default=FEATURE_TABLE)
    ap.add_argument("--label", default=LABEL)
    ap.add_argument("--test-size", type=float, default=0.25)
    ap.add_argument("--seed", type=int, default=17)
    args = ap.parse_args()

    catalog_name = f"{CATALOG_PREFIX}.{args.feature_table}"
    not_features = NOT_FEATURES | {args.label}

    mlflow.set_tracking_uri(TRACKING)
    mlflow.set_experiment(args.experiment)

    frame = read_features(args.feature_table)
    if args.label not in frame.columns:
        raise SystemExit(f"{args.feature_table} has no column named {args.label!r}")
    features = [c for c in frame.columns if c not in not_features]
    print(f"training on {len(frame):,} rows, {len(features)} features")
    print(f"  {', '.join(features)}")

    X = frame[features].copy()
    # text columns become categoricals the booster can split on directly, rather
    # than being one hot encoded into a shape that no longer resembles the table
    for column in X.columns:
        if X[column].dtype == object:
            X[column] = X[column].astype("category")
    y = frame[args.label]
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=args.test_size, random_state=args.seed, stratify=y
    )

    with mlflow.start_run() as run:
        # the edge that makes the model a node with an upstream rather than an
        # orphan. the name has to be the catalog's name for the table, because that
        # is what the urn is built from downstream.
        mlflow.log_input(
            mlflow.data.from_pandas(
                frame,
                source=WarehouseTableSource(table=catalog_name, connection=WAREHOUSE),
                name=catalog_name, targets=args.label,
            ),
            context="training",
        )

        model = HistGradientBoostingClassifier(
            max_iter=200, learning_rate=0.1, random_state=args.seed,
            categorical_features="from_dtype",
        )
        model.fit(X_train, y_train)

        predicted = model.predict(X_test)
        scored = model.predict_proba(X_test)[:, 1]
        accuracy = accuracy_score(y_test, predicted)
        auc = roc_auc_score(y_test, scored)

        mlflow.log_params({
            "max_iter": 200, "learning_rate": 0.1, "seed": args.seed,
            "n_features": len(features), "n_rows": len(frame),
            # read back by reconstruct.py, which needs to know which table to join
            # against without being told which sector it is looking at
            "feature_table": args.feature_table,
            "label": args.label,
        })
        mlflow.log_metrics({"accuracy": accuracy, "roc_auc": auc})
        mlflow.set_tag("features", ",".join(features))

        mlflow.sklearn.log_model(
            model, artifact_path="model", registered_model_name=args.model_name,
            input_example=X_train.head(3),
        )

        print(f"  run {run.info.run_id}")
        print(f"  accuracy {accuracy:.4f}  roc_auc {auc:.4f}")
        print(f"  registered as {args.model_name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
