# Ariadne

**Lineage-grounded root cause for production machine learning, built on [DataHub](https://datahub.com).**

Machine learning observability tells you a model degraded. It cannot tell you why,
because it has no idea where the model's features came from. DataHub knows exactly
where they came from, and does not watch the model.

Ariadne closes that gap. It reads DataHub's end to end ML lineage, from raw columns
through transformations and feature tables to training runs, models and deployments,
and answers the questions that only the graph can answer:

- **Which models does this change break?** Before it lands, not after.
- **What actually caused this model to move?** A named upstream change with a
  timestamp, not a distribution plot.
- **Is a protected attribute reaching a model that decides something about a person?**
  A structural fact, invisible to any monitor that only watches distributions.
- **Can the model rebuild an attribute it does not contain?** Removing a column is
  only a control if what remains does not carry the same information.

> **High risk obligations under the EU AI Act attach on 2 August 2026.** Article 10
> requires examination for bias, Article 9 requires it continuously rather than
> once, and Annex IV requires documented data provenance. Ariadne produces all
> three from the catalog rather than from a written annex that drifts.
> **[How Ariadne maps to the Act, article by article](EU_AI_ACT.md)**

## Why lineage rather than statistics

Statistical monitoring answers questions about the model. A large class of failures
is not about the model at all. It is about what reached the model, and it lives in
the space between teams, pipelines and assumptions that nobody owns until something
has already gone wrong. That space is a graph. Ariadne walks it.

[`examples/`](examples/) has the sharp version, captured against a running catalog
rather than written by hand. One line is added to a dbt model. Accuracy moves by
0.0001, row count is identical, dbt passes, no test fails, and a prohibited feature
is now an input to a deployed classifier. There is no metric for a monitor to fire
on. The only artefact that knows is the column graph.

## What is in this repo

A working reference stack, not a mock. Real US Census microdata flows through a real
dbt warehouse into a real MLflow model, all of it catalogued in DataHub, and the
checks run against that catalog.

```
  Census ACS PUMS          pipeline/extract.py     folktables, straight from the Bureau
        |
  postgres raw_*           three source tables that have to be joined
        |
  dbt staging              warehouse/models/staging
        |
  dbt marts                dim_person carries nine protected attributes
        |                  income_features is where the demo change lands
        |
  MLflow run + model       ml/train.py, with log_input naming the feature table
        |
  DataHub                  ingest/*.yml publishes all three layers
        |
  Ariadne                  tools/ walks the result
```

## Quickstart

Requires a running DataHub, a postgres warehouse and an MLflow tracking server.
Defaults assume all three on localhost.

```bash
pip install folktables pandas psycopg2-binary mlflow scikit-learn dbt-postgres

python pipeline/extract.py --states CA --year 2018   # land raw census tables
cd warehouse && dbt build && dbt docs generate && cd ..
python ml/train.py                                   # train and register

datahub ingest -c ingest/postgres.yml
datahub ingest -c ingest/dbt.yml
datahub ingest -c ingest/mlflow.yml

python tools/verify.py                               # is the thread intact?
python tools/sentinel.py                             # does any invariant fire?
```

## Commands

```bash
# what does the catalog actually know
python tools/graph.py find income_features
python tools/graph.py thread <urn>              # full chain, both directions

# follow one column back to where it came from
python tools/trace.py columns income_features
python tools/trace.py column income_features household_income_usd
python tools/trace.py model <model urn>         # protected attributes reaching a model

# structural invariants, for a person or for a pipeline
python tools/sentinel.py
python tools/sentinel.py --json
python tools/sentinel.py --fail-on-violation

# can the deployed model rebuild an attribute it does not contain
python tools/reconstruct.py --model income-classifier --repeats 3
python tools/reconstruct.py --model income-classifier --per-feature race_code

# record that figure, and fire when it moves further than noise allows
python tools/exposure.py record
python tools/exposure.py check --fail-on-violation
python tools/exposure.py history
```

`--fail-on-violation` exits non zero, so `sentinel.py` drops into CI as a gate on a
dbt or training pipeline.

## Configuration

| Variable | Default | Used by |
| --- | --- | --- |
| `DATAHUB_GMS_URL` | `http://localhost:8080` | every tool |
| `DATAHUB_TOKEN` | unset | every tool, when the instance requires auth |
| `ARIADNE_WAREHOUSE_URL` | `postgresql://ariadne:ariadne@localhost:5433/warehouse` | extract, train |
| `MLFLOW_TRACKING_URI` | `http://localhost:5000` | train |

## Layout

| Path | What it is |
| --- | --- |
| `pipeline/extract.py` | Pulls ACS PUMS person, household and geography records into postgres |
| `warehouse/` | dbt project. Staging models rename PUMS codes, marts join and derive |
| `ml/train.py` | Trains and registers the income classifier |
| `ml/warehouse_source.py` | MLflow dataset source for a warehouse table, so lineage points at the real table |
| `ingest/` | DataHub recipes for the postgres, dbt and mlflow layers |
| `examples/` | Real captured `sentinel.py` runs either side of the demo change |
| `tools/graph.py` | Thin read-only window on the DataHub graph |
| `tools/trace.py` | Column level traversal, sibling resolution, protected attribute reachability |
| `tools/sentinel.py` | Structural invariants over the graph |
| `tools/reconstruct.py` | Measures whether the features can rebuild an attribute the model excludes |
| `tools/exposure.py` | Records that measurement over time and fires when it moves |
| `tools/verify.py` | Post-ingest checks that the catalog holds what it should |
| `state/exposure.json` | Measurement history, which is the Article 12 record keeping trail |
| `EU_AI_ACT.md` | How each check maps onto the Act, and what it does not cover |

For how the traversal actually works, and the three graph shapes that make a naive
walk give a confident wrong answer, see [TECHNICAL.md](TECHNICAL.md).

## Status

Built for the DataHub Agent Hackathon, Challenge 3, Production ML Agents.

Working today: the full stack above, six hop lineage from raw census columns to a
registered model, column level traversal across siblings, the protected attribute
invariant with phantom node warnings, the reconstruction measurement, and the delta
watch. All proven end to end on captured runs in [`examples/`](examples/).

Note that `income_features` currently ships with `race_code` and `puma_code`
selected. Both are demo changes, committed deliberately so the findings reproduce.
They demonstrate different failures and are caught by different checks: `race_code`
arrives tagged and the invariant names it, while `puma_code` is tagged as personal
data and not as a protected attribute, so nothing reading tags can see it and only
the delta watch does. Remove those two lines to get the clean state back.

Writing findings back into DataHub as incidents on the affected model is designed
and not yet built.

## License

Apache 2.0.
