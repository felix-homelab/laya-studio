"""The review queue: where low-confidence decisions wait for a human."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..annotations import DESTRUCTIVE, READ, WRITE
from ..panel import Panel


def register(server: MCPServer, panel: Panel) -> None:
    @server.tool(annotations=READ)
    async def review_items(
        status_filter: str | None = None,
        workflow_id: int | None = None,
        reason: str | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Any:
        """Items parked by the ladder. `status_filter` is pending / in_review /
        approved / corrected / rejected; `reason` is why it was parked (low
        confidence, a failed verification, an unavailable fallback).
        Returns `{count, items}`."""
        return await panel.get_collection(
            "/api/review/items",
            tool="review_items",
            params={
                "status": status_filter,
                "workflow_id": workflow_id,
                "reason": reason,
                "search": search,
                "limit": limit,
                "offset": offset,
            },
        )

    @server.tool(annotations=READ)
    async def review_item_get(item_id: int) -> Any:
        """One review item: the state, the model's choice and confidence, the
        alternatives it rejected, and why it was escalated."""
        return await panel.get(f"/api/review/items/{item_id}", tool="review_item_get")

    @server.tool(annotations=WRITE)
    async def review_claim(item_id: int, assignee: str) -> Any:
        """Take an item, marking it in_review so a second reviewer does not start."""
        return await panel.post(
            f"/api/review/items/{item_id}/claim",
            tool="review_claim",
            json={"assignee": assignee},
        )

    @server.tool(annotations=WRITE)
    async def review_resolve(
        item_id: int,
        status: str,
        corrected_label: str | None = None,
        notes: str = "",
        assignee: str | None = None,
    ) -> Any:
        """Close an item out. `status` is approved / corrected / rejected / in_review,
        and a `corrected_label` is required to correct.

        Approving or correcting writes a ground-truth label back onto the
        evaluation, which is what feeds the calibration monitor — this is the tool
        that turns human review into better thresholds.
        """
        return await panel.post(
            f"/api/review/items/{item_id}/resolve",
            tool="review_resolve",
            json={
                "status": status,
                "corrected_label": corrected_label,
                "notes": notes,
                "assignee": assignee,
            },
        )

    @server.tool(annotations=DESTRUCTIVE)
    async def review_retry(item_id: int) -> Any:
        """Re-run the owning workflow against the same state, after a correction.

        Has the workflow's real side effects: this can post a webhook or send email.
        """
        return await panel.post(f"/api/review/items/{item_id}/retry", tool="review_retry")

    @server.tool(annotations=READ)
    async def review_stats() -> Any:
        """Queue depth, breakdown by status and reason, and average wait."""
        return await panel.get("/api/review/stats", tool="review_stats")
