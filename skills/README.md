# Skills

A skill is not code. It is the judgment an agent needs to use a tool well, in
the format DataHub's skills registry expects: a directory holding a `SKILL.md`
with frontmatter that says when to reach for it.

## datahub-ml-impact

DataHub's registry already covers lineage, search, enrichment and quality. None
of them cover the question this project is about: does a change to a warehouse
column reach a model that is serving decisions, and does the answer mean
anything under a governance regime.

That question has four ways of failing quietly, and all four were hit while
building this repo rather than imagined:

- tags live on the dbt sibling and traversal returns the warehouse sibling, so
  asking the wrong one returns a confident no
- filtering a column walk by column name drops the origin, because a warehouse
  renames as it cleans
- column edges stop at the feature table, so a walk that does not cross into
  the model reports that a restricted column reaches nothing
- the environment in a model urn is not a deployment status, so treating it as
  one reports every archived experiment as live

The skill encodes the workflow and each of those traps, so an agent following it
gets the answer rather than the plausible version of it.

## Installing

Skills are plain markdown and portable across agents that read them. Install
this one alongside DataHub's own:

```bash
npx skills add datahub-project/datahub-skills
npx skills add cnpierrepapi/ariadne --path skills
```

Or copy `skills/datahub-ml-impact/` into wherever the agent you use keeps its
skills.

The workflow it describes runs on DataHub's own tools, the CLI and the MCP
server, so it stands alone and does not need anything from this repository. The
implementation of the same workflow, if you want to read one, is in
`tools/blast.py` and `tools/rootcause.py`.
