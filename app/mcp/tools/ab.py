"""A/B testing: one dataset through two immutable schema revisions."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..annotations import DESTRUCTIVE, READ, WRITE
from ..panel import Panel


def register(server: MCPServer, panel: Panel) -> None:
    @server.tool(annotations=WRITE)
    async def ab_test_create(
        name: str,
        schema_id: int,
        revision_a_id: int,
        revision_b_id: int,
        dataset: list[dict[str, Any]] | None = None,
        source_job_id: int | None = None,
        model: str | None = None,
    ) -> Any:
        """Run an A/B test and return it. Both revisions must belong to `schema_id`
        (see `schema_revisions`); the dataset is either inline rows like
        `[{"state": "...", "label": "billing"}]` or the rows of an existing bulk job
        via `source_job_id`.

        The `summary` separates a real improvement from probability noise with flip
        rate, net gain and per-label transitions. Read those before believing a
        better-looking accuracy.
        """
        return await panel.post(
            "/api/ab/tests",
            tool="ab_test_create",
            json={
                "name": name,
                "schema_id": schema_id,
                "revision_a_id": revision_a_id,
                "revision_b_id": revision_b_id,
                "dataset": dataset,
                "source_job_id": source_job_id,
                "model": model,
            },
        )

    @server.tool(annotations=READ)
    async def ab_tests(schema_id: int | None = None, limit: int = 50) -> Any:
        """Recent A/B tests, newest first. Returns `{count, items}`."""
        return await panel.get_collection(
            "/api/ab/tests", tool="ab_tests", params={"schema_id": schema_id, "limit": limit}
        )

    @server.tool(annotations=READ)
    async def ab_test_get(test_id: int) -> Any:
        """One A/B test with its summary and per-row choices from both revisions."""
        return await panel.get(f"/api/ab/tests/{test_id}", tool="ab_test_get")

    @server.tool(annotations=WRITE)
    async def ab_test_run(test_id: int) -> Any:
        """Re-run a stored A/B test against the same dataset."""
        return await panel.post(f"/api/ab/tests/{test_id}/run", tool="ab_test_run")

    @server.tool(annotations=DESTRUCTIVE)
    async def ab_test_delete(test_id: int) -> Any:
        """Delete an A/B test and its rows."""
        return await panel.delete(f"/api/ab/tests/{test_id}", tool="ab_test_delete")
