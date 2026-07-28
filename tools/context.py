"""Read DataHub through its own agent surfaces, not through our own queries.

Ariadne started by talking to GraphQL directly, which was the right way to prove
the graph existed but the wrong way to build agents on it. DataHub ships two
supported ways for an agent to read a catalog, and an agent that bypasses both is
asserting things about a graph through a private door.

So everything an agent needs goes through here, and there are two transports:

  kit   the Agent Context Kit in process. `pip install datahub-agent-context`,
        call the same functions the MCP server calls. Fast, no subprocess.
  mcp   the real MCP server as a child process over stdio, spoken to in JSON-RPC
        exactly as any MCP client would. Slower, and the honest one, because it
        is the path a third party agent would actually take.

Both are real and both are wired. The agents take `--via` and default to the kit
for speed, and `tools/agree.py` runs a question down both and checks the answers
match, which is the only way to be sure the fast path is not quietly different.

A note on why the transports can disagree and it not be a bug: the MCP server
trims its responses to keep an answer inside a model's context window, so a field
present in the kit response can be absent over MCP. Anything read here is read
from fields both keep.

    DATAHUB_GMS_URL   defaults to http://localhost:8080
    DATAHUB_GMS_TOKEN optional, required if metadata service auth is on
    ARIADNE_MCP_BIN   path to the mcp-server-datahub executable, for --via mcp
"""

from __future__ import annotations

import asyncio
import json
import os
import threading

GMS = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
TOKEN = os.environ.get("DATAHUB_GMS_TOKEN") or os.environ.get("DATAHUB_TOKEN")
MCP_BIN = os.environ.get(
    "ARIADNE_MCP_BIN", os.path.expanduser("~/ariadne/av/bin/mcp-server-datahub")
)


class Context:
    """One question shape, two transports. Subclasses implement `call`."""

    via = "?"

    def call(self, tool: str, args: dict) -> dict:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- the three questions Ariadne actually asks -----------------------

    def search(self, query: str, num_results: int = 10) -> list[dict]:
        got = self.call("search", {"query": query, "num_results": num_results})
        return [r.get("entity") or {} for r in got.get("searchResults") or []]

    def lineage(self, urn: str, column: str | None = None, upstream: bool = True,
                max_hops: int = 3, max_results: int = 100) -> list[dict]:
        """Neighbours in one direction, flattened to what a check needs.

        The raw response carries facet aggregations for a UI to draw with, which
        is most of its size and none of its meaning here.
        """
        args = {"urn": urn, "upstream": upstream, "max_hops": max_hops,
                "max_results": max_results}
        if column:
            args["column"] = column
        got = self.call("get_lineage", args)
        side = got.get("upstreams" if upstream else "downstreams") or {}
        out = []
        for row in side.get("searchResults") or []:
            entity = row.get("entity") or {}
            out.append({
                "urn": entity.get("urn"),
                "type": entity_type(entity),
                "name": entity.get("name"),
                "platform": ((entity.get("platform") or {}).get("name")),
                "degree": row.get("degree"),
                # which column carried the edge. absent on table level edges.
                "columns": row.get("lineageColumns") or [],
                "tags": _tags(entity),
                "health": _health(entity),
                "subtypes": ((entity.get("subTypes") or {}).get("typeNames") or []),
                # the dbt connector carries the source file, which is the
                # difference between naming a table and naming the change
                "file": _custom(entity).get("dbt_file_path"),
            })
        out.sort(key=lambda r: (r["degree"] if r["degree"] is not None else 99,
                                r["urn"] or ""))
        return out

    def schema_fields(self, urn: str, limit: int = 200) -> list[dict]:
        got = self.call("list_schema_fields", {"urn": urn, "limit": limit})
        return got.get("fields") or []


def entity_type(entity: dict) -> str:
    """What kind of thing this is, taken from the urn rather than the field.

    The MCP server trims its responses so an answer fits in a model's context
    window, and `type` is one of the fields it drops from search results. The
    kit keeps it. Reading the field therefore works perfectly through one
    transport and matches nothing through the other, with no error either way.

    The urn carries the same fact and no transport can afford to trim it, since
    it is the identifier. So it is the more reliable source, and using it means
    the two surfaces answer the same question rather than nearly the same one.
    """
    declared = entity.get("type")
    if declared:
        return declared
    urn = entity.get("urn") or ""
    if not urn.startswith("urn:li:"):
        return "UNKNOWN"
    kind = urn.split(":")[2]
    # urn:li:mlModel -> MLMODEL, urn:li:dataProcessInstance -> DATA_PROCESS_INSTANCE
    if kind.lower().startswith("ml"):
        return kind.upper()
    spaced = "".join(f"_{c}" if c.isupper() else c for c in kind)
    return spaced.upper().strip("_")


def _custom(entity: dict) -> dict[str, str]:
    return {
        p.get("key"): p.get("value")
        for p in ((entity.get("properties") or {}).get("customProperties") or [])
    }


def _tags(entity: dict) -> list[str]:
    return [
        (t.get("tag") or {}).get("urn", "").rsplit(":", 1)[-1]
        for t in ((entity.get("tags") or {}).get("tags") or [])
    ]


def _health(entity: dict) -> list[str]:
    """Whatever DataHub already believes is wrong with this entity.

    Worth carrying because incidents Ariadne raised earlier come back here, so a
    later run can see its own past findings without being told about them.
    """
    return [f"{h.get('type')}:{h.get('status')} {h.get('message', '')}".strip()
            for h in (entity.get("health") or [])]


# ---------------------------------------------------------------------------
# transport 1: the Agent Context Kit, in process
# ---------------------------------------------------------------------------

class KitContext(Context):
    via = "kit"

    def __init__(self) -> None:
        from datahub.sdk import DataHubClient
        from datahub_agent_context import set_client

        set_client(DataHubClient(server=GMS, token=TOKEN))
        # the submodules are imported by path rather than from the package,
        # because the package re-exports the tool functions under the same names
        # as the modules that hold them
        from datahub_agent_context.mcp_tools.entities import list_schema_fields
        from datahub_agent_context.mcp_tools.lineage import get_lineage
        from datahub_agent_context.mcp_tools.search import search

        self._tools = {
            "search": search,
            "get_lineage": get_lineage,
            "list_schema_fields": list_schema_fields,
        }

    def call(self, tool: str, args: dict) -> dict:
        return self._tools[tool](**args)


# ---------------------------------------------------------------------------
# transport 2: the MCP server, over stdio
# ---------------------------------------------------------------------------

class MCPContext(Context):
    """The official MCP server as a child process, spoken to as a real client.

    The session has to stay open across calls or every question pays the server
    startup cost, and MCP sessions are async while the checks are not. So the
    loop runs on its own thread and calls are handed to it. Ugly in the middle,
    ordinary at both ends.
    """

    via = "mcp"

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=120)
        if self._error:
            raise SystemExit(f"mcp server would not start: {self._error}")

    _error: str | None = None

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as exc:  # surfaced on the calling thread
            self._error = f"{type(exc).__name__}: {exc}"
            self._ready.set()

    async def _serve(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = dict(os.environ)
        env["DATAHUB_GMS_URL"] = GMS
        if TOKEN:
            env["DATAHUB_GMS_TOKEN"] = TOKEN
        # The server posts a usage ping before it answers initialize. On a box
        # that cannot reach that host the send retries for about forty seconds,
        # and because it happens before the handshake an MCP client sees no error
        # and no response, just a stall. Cost an afternoon to find.
        env["DATAHUB_TELEMETRY_ENABLED"] = "false"

        params = StdioServerParameters(command=MCP_BIN,
                                       args=["--transport", "stdio"], env=env)
        self._stop = asyncio.Event()
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                info = await session.initialize()
                self.server = f"{info.serverInfo.name} {info.serverInfo.version}"
                listed = await session.list_tools()
                self.tools = sorted(t.name for t in listed.tools)
                self._session = session
                self._ready.set()
                await self._stop.wait()

    def call(self, tool: str, args: dict) -> dict:
        async def go():
            return await self._session.call_tool(tool, args)

        result = asyncio.run_coroutine_threadsafe(go(), self._loop).result(180)
        text = "".join(getattr(c, "text", "") for c in result.content)
        if result.isError:
            raise SystemExit(f"mcp {tool} failed: {text[:400]}")
        return json.loads(text) if text.strip() else {}

    def close(self) -> None:
        if getattr(self, "_stop", None):
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=15)


# A table exists three times over: dbt holds the governance tags, postgres holds
# the physical table, mlflow holds the training frame. Lineage traversal returns
# postgres urns, so a walk that starts from the dbt sibling starts in a different
# copy of the graph and quietly returns a shorter answer.
PLATFORM_PREFERENCE = ("postgres", "dbt", "mlflow")


def resolve_dataset(ctx: Context, name: str) -> str:
    """Turn a table name into the one urn a downstream walk should start from."""
    wanted = name.lower()
    hits = [
        h for h in ctx.search(name, num_results=30)
        if entity_type(h) == "DATASET" and wanted in (h.get("urn") or "").lower()
    ]
    if not hits:
        raise SystemExit(f"no dataset in the catalog matching {name!r}")
    for platform in PLATFORM_PREFERENCE:
        for hit in hits:
            if f"dataPlatform:{platform}," in hit["urn"]:
                return hit["urn"]
    return hits[0]["urn"]


def open_context(via: str = "kit") -> Context:
    if via == "kit":
        return KitContext()
    if via == "mcp":
        return MCPContext()
    raise SystemExit(f"unknown transport {via!r}, expected kit or mcp")


def add_transport_arg(parser) -> None:
    parser.add_argument("--via", choices=("kit", "mcp"), default="kit",
                        help="read DataHub through the Agent Context Kit in "
                             "process, or through the MCP server over stdio")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="show what the transport can see")
    add_transport_arg(ap)
    args = ap.parse_args()
    with open_context(args.via) as ctx:
        print(f"transport {ctx.via}, gms {GMS}")
        if ctx.via == "mcp":
            print(f"server    {ctx.server}")
            print(f"tools     {len(ctx.tools)}: {', '.join(ctx.tools)}")
        hits = ctx.search("workforce_features", num_results=5)
        print(f"\nsearch workforce_features: {len(hits)} entities")
        for h in hits:
            print(f"  {h.get('urn')}")
