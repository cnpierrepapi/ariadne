"""Train and register the income classifier on the warehouse feature table.

Deliberately ordinary. This is what an ML engineer writes on a Tuesday: pull the
feature table, split, fit a gradient boosting model, log the run, register the
version. No tricks, because the point of Ariadne is that nothing about this file
has to change for the lineage to be watchable.

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
# how the catalog names this table: database included, because the urn is built
# from the fully qualified name and the two have to line up
CATALOG_NAME = f"warehouse.{FEATURE_TABLE}"
LABEL = "income_above_50k"
NOT_FEATURES = {"person_id", "survey_year", LABEL}


def read_features() -> pd.DataFrame:
    with psycopg2.connect(WAREHOUSE) as conn:
        return pd.read_sql(f"select * from {FEATURE_TABLE}", conn)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--experiment", default="income-classifier")
    ap.add_argument("--model-name", default="income-classifier")
    ap.add_argument("--test-size", type=float, default=0.25)
    ap.add_argument("--seed", type=int, default=17)
    args = ap.parse_args()

    mlflow.set_tracking_uri(TRACKING)
    mlflow.set_experiment(args.experiment)

    frame = read_features()
    features = [c for c in frame.columns if c not in NOT_FEATURES]
    print(f"training on {len(frame):,} rows, {len(features)} features")
    print(f"  {', '.join(features)}")

    X = frame[features].copy()
    # text columns become categoricals the booster can split on directly, rather
    # than being one hot encoded into a shape that no longer resembles the table
    for column in X.columns:
        if X[column].dtype == object:
            X[column] = X[column].astype("category")
    y = frame[LABEL]
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
                source=WarehouseTableSource(table=CATALOG_NAME, connection=WAREHOUSE),
                name=CATALOG_NAME, targets=LABEL,
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
            "feature_table": FEATURE_TABLE,
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
