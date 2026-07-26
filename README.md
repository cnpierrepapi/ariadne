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

Findings are written back into DataHub as incidents on the affected model, so the
next person to open it inherits the knowledge.

## Why lineage rather than statistics

Around 40 percent of production ML failures trace to feature mismatches, and the
post mortems all say the same thing: the bug lives in the space between teams,
pipelines and assumptions, and nobody owns it until something has already gone
wrong. That space is a graph. Ariadne walks it.

## Status

Under construction for the DataHub Agent Hackathon. Phase 0 complete: end to end
lineage proven from raw US Census columns to a registered model.

## License

Apache 2.0.
