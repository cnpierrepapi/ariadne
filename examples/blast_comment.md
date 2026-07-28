$ python tools/blast.py analytics_marts.dim_person --column public_coverage_flag --policy eu_ai_act --comment
**Blast radius: `analytics_marts.dim_person.public_coverage_flag`**

> **examine**: in scope because it carries personal_data, and EU AI Act watches that tag. Under Article 10(2)(f), Article 10(2)(g), Article 9, Annex IV.

Reaches **1 deployed model**:

| model | stage | hops |
| --- | --- | --- |
| `workforce-classifier_3` | mlflow_production | 5 |

<details><summary>2 downstream tables</summary>

- 1 hop, `workforce_features` on dbt (carries `public_coverage_flag`)
- 2 hop, `workforce_features` on postgres (carries `public_coverage_flag`)

</details>

<sub>Ariadne, from DataHub lineage, under Regulation (EU) 2024/1689, Annex III high risk systems.</sub>
