# A prohibited feature enters a live model, and nothing else notices

Captured against a running catalog, not written by hand. `before.txt` and
`after.txt` are the output of `tools/sentinel.py` on either side of a one line
change to `warehouse/models/marts/income_features.sql`. The `.json` files are the
same run with `--json`.

## The change

```sql
     marital_status_code,
     occupation_code,
     hours_worked_per_week,
+    race_code,
```

That is the whole thing. Committed with an ordinary message about a quarterly
report. The header comment at the top of the file still says the table excludes
protected attributes on purpose, because the person making the change did not
read it, which is how this happens rather than a contrived version of how it
happens.

No other file changed. `ml/train.py` selects every column except the identifier,
the survey year and the label, so a new column becomes a model input on its own.
Nobody edited the training code. Nobody reviewed a model change, because there
was no model change to review.

## What every other signal reported

| | before | after |
|---|---|---|
| features | 10 | 11 |
| accuracy | 0.8768 | 0.8767 |
| roc auc | 0.9522 | 0.9521 |
| rows | 196,604 | 196,604 |

Accuracy moved by one ten thousandth, downward. Row count identical. The dbt run
passed. No test failed anywhere, because no test asserts anything about which
columns exist.

This is the part worth sitting with. A monitor watching prediction distributions,
accuracy or drift has nothing to fire on. Not a signal pointing the wrong way, no
signal at all. Any threshold sensitive enough to catch a change of 0.0001 would
fire constantly on ordinary retraining noise, so no responsible team sets one
there. Metric based observability is not doing a bad job here. The event it would
need to see does not exist in the metrics.

## What the graph reported

```
  income-classifier_2  (mlflow_production)
  trained on warehouse.analytics_marts.income_features, 14 columns, 3 protected
    age                      tagged on the feature itself
    marital_status_code      tagged on the feature itself
    race_code                warehouse.analytics_marts.dim_person.race_code, 1 hop back
```

Note where race_code was caught. It carries no governance tag of its own on the
feature table, because the change added a column and nobody adds a tag for a
column they have not thought about. It was found by following the column back one
step to `dim_person`, where the tag does live. Table level lineage cannot make
that call: every table downstream of `dim_person` has nine protected attributes
somewhere in its history, so a check at table granularity reports a violation on
the clean version too, and then forever.

The full trail runs to the raw survey file:

```
income_features.race_code
  1 hop   dim_person.race_code    [personal_data, protected_attribute]
  2 hops  stg_person.race_code    [personal_data, protected_attribute]
  3 hops  raw_person.rac1p
```

`rac1p` is the real variable name in the American Community Survey public use
microdata. The chain is not annotated by hand at any point. Postgres, dbt and
mlflow connectors emitted all of it.

## The two findings that were already there

`before.txt` is not empty. Age and marital status were in the feature table from
the start, and both are prohibited bases under the Equal Credit Opportunity Act.
The check reports two before the change and three after.

Leaving those in is deliberate. A demo that starts from silence and ends with one
alarm is easy to arrange and proves less: it only shows the tool reacting to
something the author planted. Starting from two shows the tool finding exposures
nobody put there for it, which is what the first run inside a real organisation
looks like.

## That it stays quiet

A check that only ever fires proves nothing. Archiving the model in the registry
and changing nothing else returns:

```
== protected attribute reaches a deployed model ==
  no violations
```

Same graph, same protected columns, same trail. The finding depends on the model
actually being deployed, read from the registry stage rather than inferred from
the `PROD` in the model urn, which is the fabric the entity lives in and would
mark every registered experiment as serving.
