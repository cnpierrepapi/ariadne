"""Tell MLflow the truth about where training data came from.

MLflow resolves a dataset source from a URI, and it ships resolvers for object
stores, Delta, Spark and HTTP. It has none for a warehouse table, so logging
`source="postgresql://..."` fails outright and the usual workaround is to point at
a file or leave the source off. Both throw away the only fact that matters: which
table the model was trained on.

That fact is the edge Ariadne walks. DataHub's MLflow connector reads
`dataset.source_type` and maps it to a data platform, then uses `dataset.name` as
the dataset name, so a source type of "postgres" and a name matching the warehouse
table produce an upstream pointing at the real table rather than at a copy of it.

So this is a small honest resolver rather than a workaround: it says the training
data was a table in a warehouse, and names it.
"""

from __future__ import annotations

from typing import Any

from mlflow.data.dataset_source import DatasetSource

SOURCE_TYPE = "postgres"


class WarehouseTableSource(DatasetSource):
    """A dataset that lives as a table in the warehouse.

    `table` is the fully qualified name as the catalog knows it, database included,
    because that is what has to line up with the urn DataHub mints for the table.
    """

    def __init__(self, table: str, connection: str = ""):
        self.table = table
        # kept for provenance, never used to reconnect, and no credentials belong
        # in a run's metadata: strip anything before the host
        self.connection = connection.rsplit("@", 1)[-1] if connection else ""

    @staticmethod
    def _get_source_type() -> str:
        return SOURCE_TYPE

    def load(self, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "the warehouse is the system of record; read the table directly"
        )

    @staticmethod
    def _can_resolve(raw_source: Any) -> bool:
        return isinstance(raw_source, str) and raw_source.startswith("warehouse://")

    @classmethod
    def _resolve(cls, raw_source: str) -> "WarehouseTableSource":
        return cls(table=raw_source[len("warehouse://"):])

    def to_dict(self) -> dict[str, Any]:
        return {"table": self.table, "connection": self.connection}

    @classmethod
    def from_dict(cls, source_dict: dict[str, Any]) -> "WarehouseTableSource":
        return cls(table=source_dict["table"],
                   connection=source_dict.get("connection", ""))
