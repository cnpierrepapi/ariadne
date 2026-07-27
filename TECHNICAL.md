# Ariadne, technical notes

How the traversal works, why it is built the way it is, and the graph shapes that
make a naive implementation give a confident wrong answer.

## The claim

Ariadne makes exactly one kind of claim: a statement about the DataHub graph. Not
about a distribution, not about a metric, not about model behaviour. That constraint
is deliberate, and it is what makes the findings actionable, because a statement
about the graph names a table, a column and a number of hops.

Everything here is therefore in service of one question: **can this column be
reached from that column, and what is true of it along the way.**

## Stack

| Layer | Technology | Notes |
| --- | --- | --- |
| Source data | US Census ACS PUMS via `folktables` | Real microdata, fetched from the Bureau, never generated |
| Warehouse | postgres 15 | Three raw tables, two dbt schemas on top |
| Transformation | dbt-postgres | Emits column level lineage into the manifest |
| Training | scikit-learn `HistGradientBoostingClassifier` | Deliberately ordinary |
| Tracking and registry | MLflow | Registry stage is the deployment signal |
| Catalog | DataHub | Three ingest recipes, one per layer |
| Traversal | Python 3.10+, stdlib only | `urllib` against `/api/graphql`, no SDK |

The tools deliberately have no dependency beyond the standard library. They talk to
DataHub over GraphQL directly, so they run anywhere Python does and never fight
DataHub's own pinned dependency tree.

## Data model

### Why three source tables

A single flat table produces a lineage graph with nothing in it. `extract.py` lands
person records, household records and a geography dimension separately, exactly as a
warehouse receives them, so the joins in `dim_person` are real joins and the
resulting column lineage is worth walking.

PUMS column codes (`AGEP`, `RAC1P`, `SERIALNO`) are kept as-is at the raw layer
rather than prettified, because an agent reading this catalog should face the same
naming a real analyst does. The staging models are where they become readable.

### Why `dim_person` keeps protected attributes

`dim_person` carries nine of them: sex, race, place of birth, citizenship, nativity,
disability and ancestry among them. That is correct. A census dimension is supposed
to carry them, and analysts have lawful reasons to use them.

`income_features` is built from `dim_person`, and its header comment says it
excludes protected attributes on purpose. The canonical ACSIncome benchmark feature
list *does* include SEX and RAC1P; this table is supposed to drop them, because a
model that decides something about a person's income must not reason from a
prohibited basis.

Nothing enforces that. It is a convention held in place by whoever last edited the
SQL, which is precisely why it needs watching from outside. `examples/` is that
convention being broken in one line while the comment asserting it stays right
there at the top of the file, unread.

The repo ships in the broken state so the violation reproduces on a fresh build.
Removing `race_code` from the select list restores the clean one.

### The edge that makes a model a node

`mlflow.log_input` in `ml/train.py` is the single line that carries weight. Without
it a registered model is an orphan: you know it exists, you cannot know what made
it. With it, the model has an upstream, and every check Ariadne performs walks
through that edge.

MLflow ships dataset source resolvers for object stores, Delta, Spark and HTTP, and
none for a warehouse table. Logging `source="postgresql://..."` fails outright, and
the usual workaround is to point at a file or omit the source, both of which throw
away the only fact that matters.

`ml/warehouse_source.py` is a small honest resolver instead. It declares
`_get_source_type() -> "postgres"`, and DataHub's MLflow connector reads
`dataset.source_type`, maps it through `source_mapping_to_platform` in
`ingest/mlflow.yml`, and uses `dataset.name` as the dataset name. Give it the fully
qualified catalog name and the model's upstream resolves to **the same entity the
warehouse already published**, not a second copy that happens to share a name.

The connection string is stored with credentials stripped (`rsplit("@", 1)[-1]`).
No credentials belong in a run's metadata.

## The three traversal problems

These are the reason `trace.py` exists and is not thirty lines long.

### 1. Table lineage cannot answer the question

Every table downstream of `dim_person` has nine protected attributes somewhere in
its history, whether it selects one or not. A check written against table lineage
cannot tell those two situations apart, so it reports a violation on the clean
version too, and then forever.

Column lineage can tell them apart. The dbt connector parses the model SQL and emits
an edge per output column, so the question becomes whether `race_code` reaches a
column the model actually trains on. That is answerable, and it stays quiet until it
is true.

The catch is that the newly added column carries no governance tag of its own.
Nobody tags a column they have not thought about. It is caught by following the
column one hop back to `dim_person`, where the tag does live, which is why
reachability rather than direct inspection is the operation that matters.

### 2. One table is several entities

`dim_person` exists twice, as a dbt dataset and as a postgres dataset, joined by a
real `siblings` aspect. `income_features` exists a third time as an mlflow dataset,
joined by an ordinary lineage edge instead.

The governance tags land on **the dbt entity only**. Traversal returns **the postgres
one**. Ask the postgres entity for its tags and it answers, truthfully, that it has
none. A check built on that answer finds nothing and says so confidently.

### 3. The column edges zigzag between those entities

The edge out of `income_features` is stored on the dbt entity and points at the
postgres `dim_person`, whose own edges are stored back on the dbt `dim_person`. Land
on a postgres node, read only that node's edges, and the walk stops early with no
error at all.

### The fix: nodes are names, not entities

Both problems dissolve if a node is a **table name** rather than an entity. Names
here are fully qualified catalog paths, so sharing one means being the same table.

`trace.py` therefore:

1. Builds `_by_name()` once, grouping every dataset entity in the catalog by the
   table it describes.
2. Unions edges (`_edges`) and tags (`tags`) across every entity that shares a name.
3. Walks over names, not urns.
4. Drops edges whose upstream and downstream are the same table. That is how the
   siblings aspect represents the dbt/postgres mirror, and it is not a real hop.

`resolve()` accepts a urn, an exact name, or a unique substring, and refuses an
ambiguous one rather than picking.

Everything expensive is memoised with `lru_cache`, so a full model check makes each
GraphQL call once.

### GraphQL quirks worth knowing

`lineage` hangs off the `EntityWithRelationships` interface, not off `Entity`, and
asking for `name` across several concrete types trips a nullability conflict. So
`graph.py` asks the interface for lineage only and reads names out of the urns. That
is why `_short()` and `dataset_name()` do urn string surgery instead of querying.

## Reachability

```
parents(table, column)      one hop up, from the unioned edge set
ancestry(table, column)     BFS to depth 12, returns {(table, column): hops}
protected_reaching(table)   for each column: is it tagged, or is anything in its
                            ancestry tagged; report the nearest tagged ancestor
```

`protected_reaching` breaks at the first tagged ancestor per column, sorted by hop
count, so a finding names the *nearest* origin rather than an arbitrary one.

`training_datasets(model_urn)` does not assume a fixed hop count. The model points at
the training run, which points at the table, and that shape can change. It walks
upstream, collects every dataset it reaches, and takes the ones at minimum hop
distance.

## Invariants

`sentinel.py` holds structural invariants. An invariant is not a threshold on a
metric. Metrics answer "is the model behaving differently than it used to", which is
a question about the model. Invariants answer "is the model wired to something it
should never have been wired to", which is a question about the graph.

### Implemented: protected attribute reaches a deployed model

Both halves matter. A protected column sitting in a warehouse table is normal and is
not a finding. The same column reaching a live decision is the thing regulation cares
about.

**Deployment is read from the registry, not inferred.** The `PROD` in a model urn is
the fabric the entity lives in, not a statement that anything is serving, and
treating it as one would report every registered experiment as deployed. The MLflow
connector writes the registry stage as a lowercased tag, so `mlflow_production` or
`mlflow_staging` means someone actually promoted it.

### Phantom nodes

`unresolved_hops()` reports upstream tables that a column edge names but no connector
ever wrote an entity for. dbt sometimes declares a source in the consuming model's
schema rather than its own, which leaves a urn with nothing behind it. A tag lookup
against it returns a confident nothing.

Today every such node has a real twin reached in parallel, so no finding is missed.
That is luck rather than design, and if it stops being true a protected column could
go unseen, so it is surfaced as a warning rather than swallowed.

### Proving it stays quiet

A check that only ever fires proves nothing. Archiving the model in the registry and
changing nothing else returns no violations against the same graph, the same
protected columns and the same trail. That is the registry-stage read doing its job,
and it is the reason the deployment signal is not inferred from the urn.

`examples/before.txt` is also not empty. Age and marital status were feature table
columns from the start and both are prohibited bases under the Equal Credit
Opportunity Act, so the check reports two before the demo change and three after.
Leaving them in is deliberate: a demo that starts from silence only shows the tool
reacting to something the author planted.

### Output modes

| Invocation | Use |
| --- | --- |
| `sentinel.py` | Human readable report |
| `sentinel.py --json` | Machine readable findings and warnings |
| `sentinel.py --fail-on-violation` | CI gate, exits 1 when anything fires |

## Verification

`verify.py` runs after any rebuild or reingest, asking three questions in order of
how badly a wrong answer would mislead everything downstream.

1. **Is the instance clean?** Anything outside the `warehouse.` namespace is
   leftover sample data or another project. This also catches a reingest that
   silently doubled entities.
2. **Is the thread whole?** The model has to reach `raw_person`. If it does not,
   every check built on the traversal is walking a graph with a hole in it and will
   report all clear for the wrong reason.
3. **Are the tags where the checks will look?** It asserts that `race_code` is
   tagged on the **dbt** entity and that the **postgres** entity carries no tags at
   all. That second assertion is the important one: it proves the sibling union in
   `trace.py` is required rather than optional, and it fails loudly if the ingest
   ever changes shape.

DataHub indexes asynchronously through Kafka, so a check run straight after ingest
can see an empty graph that fills in a minute later. `await_index()` polls to a
timeout rather than reporting a failure that is really just impatience.

## Extending

**A new invariant.** Add a function to `sentinel.py` returning findings in the same
shape, and call it from `violations()`. Reuse `trace.protected_reaching` as the
model for a reachability-based check.

**A new tag to watch.** `trace.PROTECTED` is the tag slug. Reachability is generic,
so a different governance tag needs only a different constant.

**A new source platform.** Add an ingest recipe. Because traversal keys on fully
qualified table names, a new platform publishing the same table joins the existing
node automatically rather than creating a parallel graph.

**A deeper graph.** `walk()` defaults to depth 8 and `ancestry()` to 12. Both are
parameters, not constants in the algorithm.

## Known limits

- Column lineage exists only where dbt could parse the SQL. A model built with a
  macro dbt cannot resolve produces table lineage and no column edges, and
  reachability through it is silently coarse.
- `find("*", count=500)` in `_by_name()` caps the catalog at 500 dataset entities.
  Fine for this stack, not for a large instance.
- Writing findings back into DataHub as incidents is designed and not yet built, so
  a finding lives in the terminal or in CI output rather than on the model page.
- Only one invariant is implemented. The other two questions in the README (which
  models does this change break, what caused this model to move) are answerable with
  the same traversal and are not yet wired up.
