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
- **What do you hand a regulator afterwards?** A record of one dated assessment,
  naming every measurement including the ones that found nothing, because a report
  showing only what fired cannot be told apart from one where the rest never ran.

> **The EU AI Act's high risk obligations were deferred to 2 December 2027.** The Digital
> Omnibus on AI, approved by the Council on 29 June 2026, moved standalone Annex III
> systems back by sixteen months from the original 2 August 2026, and product embedded
> systems under Annex I to 2 August 2028. The Article 50 transparency duties did take
> effect on 2 August 2026.
>
> What Articles 9, 10 and Annex IV ask for has not changed, only when it binds:
> examination for bias, repeated rather than done once, and documented data provenance.
> Ariadne produces all three from the catalog rather than from a written annex that
> drifts. The other three packs in this repository cite law that is in force today.
> **[How Ariadne maps to the Act, article by article](EU_AI_ACT.md)**

**[ariadne-five.vercel.app](https://ariadne-five.vercel.app)** walks the whole thing
against captured runs, and **[/demo](https://ariadne-five.vercel.app/demo)** is the
six step version: the estate, the daily runs, the rebuild, where the column came
from, the writeback with live links into DataHub, and the document that comes out at
the end. Every figure on it was measured. Nothing on it was drawn.

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

# order matters, and postgres runs twice. see below
datahub ingest -c ingest/postgres.yml
datahub ingest -c ingest/dbt.yml
datahub ingest -c ingest/mlflow.yml
datahub ingest -c ingest/postgres.yml

python tools/verify.py                               # is the thread intact?
python tools/sentinel.py                             # does any invariant fire?

# file what it found back into the catalog. nothing is written without --raise
python tools/incident.py --model workforce-classifier --policy employment_us --raise

# the record the operator hands a regulator for that run
python tools/complydoc.py --model workforce-classifier --policy eu_ai_act \
    --operator "Acme Financial" --out report.pdf
```

### Why postgres is ingested twice

Two ordering constraints that point opposite ways, both found by running into them.

The dbt source resolves `select *` against the schema DataHub already holds for the
target platform, not against dbt's own catalog. So a column added since the last
postgres ingest is invisible to it, and the column level edge for that column is
silently missing while every other column in the same table traces correctly.
**Postgres has to run before dbt.**

The mlflow source overwrites the postgres entity's schema with the columns of the
training frame. Run it last and the catalog shows a feature table with the columns
the model happened to train on rather than the columns the table has.
**Postgres has to run after mlflow.**

Neither failure raises an error. The first loses one lineage edge, the second
leaves a table looking correct and reading wrong.

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

# read the catalog through DataHub's own agent surfaces, either transport
python tools/context.py --via mcp                # what the MCP server offers
python tools/agree.py                            # do the two surfaces agree

# the two agents
python tools/blast.py analytics_marts.dim_person --column public_coverage_flag \
    --policy eu_ai_act --comment                 # before the change lands
python tools/rootcause.py --model workforce-classifier --via mcp   # after it did

# measure the declared proxy hypotheses, and let most of them fail
python tools/screen.py

# work out what a column holds with no tags at all
python tools/identify.py public.raw_person

# which statutes are declared, and what one of them makes of this warehouse
python tools/policy.py list
python tools/policy.py show canada

# file findings as incidents, and produce the record for that run
python tools/incident.py --model income-classifier --policy ecoa --raise
python tools/complydoc.py --model income-classifier --policy canada --out report.pdf
```

### The same warehouse, read four ways

`policy/attributes.yml` describes the warehouse, which is true under any law. Each
regime file describes a law and names **attributes, never columns**, so a pack moves
to a different warehouse unchanged. Nothing about any statute is compiled into a tool.

| Pack | The law | Restricted |
| --- | --- | --- |
| `canada` | Canadian Human Rights Act RSC 1985 c. H-6 s.3 | 9, all prohibited |
| `ecoa` | Equal Credit Opportunity Act, 15 USC 1691 | 8 |
| `employment_us` | Title VII, ADEA, ADA, 41 CFR 60-3 | 9 |
| `eu_ai_act` | Regulation (EU) 2024/1689, Annex III | 9 |

The disagreement is the point. `age` reads prohibited under Canada, conditional
under ECOA, prohibited under US employment law and examine under the AI Act.
`marital_status_code` reads prohibited, prohibited, examine, examine. One column,
one graph, four answers, and each one cites the provision it came from.

Each pack also declares a `duties:` block, which is the obligation attaching to the
**decision** rather than to a column: the adverse action notice under 15 USC 1691(d),
Article 86 explanation, Quebec Law 25 s.12.1. Each states what the run supplies and,
by name, what it does not.

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
| `tools/history.py` | The recording history, and the single definition of what counts as a change |
| `tools/screen.py` | Measures the declared proxy hypotheses, and lets most of them fail |
| `tools/identify.py` | Works out what a column holds when it carries no tags at all |
| `tools/policy.py` | Loads a regime and resolves it against the warehouse |
| `tools/incident.py` | Files findings back into DataHub, regime named in the title |
| `tools/complydoc.py` | The per run record an operator hands a regulator |
| `tools/blast.py` | Before the change lands: which models does this break |
| `tools/rootcause.py` | After it did: what actually moved this model |
| `tools/context.py`, `tools/agree.py` | DataHub's own agent surfaces, and whether the two agree |
| `tools/verify.py` | Post-ingest checks that the catalog holds what it should |
| `policy/` | `attributes.yml` describes the warehouse, one file per statute describes a law |
| `site/` | The deployed walkthrough, including `site/docs/` with three generated records |
| `skills/` | The ML impact skill, filed upstream as datahub-skills#66 |
| `state/exposure.json` | Measurement history, which is the Article 12 record keeping trail |
| `EU_AI_ACT.md` | How each check maps onto the Act, and what it does not cover |

For how the traversal actually works, and the three graph shapes that make a naive
walk give a confident wrong answer, see [TECHNICAL.md](TECHNICAL.md).

## Contributed back

Four things this build hit that were not in the documentation. Each one fails
silently, which is why they were worth writing up rather than working around.

| | Where |
| --- | --- |
| The self-hosted MCP server stalls before answering `initialize` where its telemetry host is unreachable, and the trimmed responses omit `type` from search results | [datahub#18684](https://github.com/datahub-project/datahub/pull/18684) |
| `raiseIncident` accepts seven entity types and no ML entity, where the docs said "dataset, dashboard, chart, dataFlow, etc" | [datahub#18685](https://github.com/datahub-project/datahub/pull/18685) |
| An ML impact skill, covering the question the existing five skills do not, with four quiet failure modes written into it | [datahub-skills#66](https://github.com/datahub-project/datahub-skills/pull/66) |
| `datahub-agent-context` cannot import its own LangChain registration against the `acryl-datahub` version it pins | [datahub#18686](https://github.com/datahub-project/datahub/issues/18686) |

## Status

Built for the DataHub Agent Hackathon, Challenge 3, Production ML Agents.

Working today: the full stack above, seven hop column lineage from raw census
columns to a registered model, column level traversal across siblings, the
protected attribute invariant with phantom node warnings, the reconstruction
measurement, the delta watch, four statutory regimes over the one warehouse,
findings filed back into DataHub as incidents, and the per run compliance record.
All proven end to end on captured runs in [`examples/`](examples/).

Note that `income_features` currently ships with `race_code` and `puma_code`
selected. Both are demo changes, committed deliberately so the findings reproduce.
They demonstrate different failures and are caught by different checks: `race_code`
arrives tagged and the invariant names it, while `puma_code` is tagged as personal
data and not as a protected attribute, so nothing reading tags can see it and only
the delta watch does. Remove those two lines to get the clean state back.

Findings are written back into DataHub as incidents. **They are filed on the feature
table, not on the model**, because `raiseIncident` accepts no ML entity type at all,
which is the subject of [datahub#18685](https://github.com/datahub-project/datahub/pull/18685).
The reference instance currently holds 18 active incidents across two feature tables,
and the incident title names the regime, so the same column can be open at two
severities under two statutes at once. Nothing is written without `--raise`.

Not built: the nightly schedule. The documents and the incidents are produced by a
command today, not unattended, and [`/demo`](https://ariadne-five.vercel.app/demo)
says so in the same words.

## License

Apache 2.0.
