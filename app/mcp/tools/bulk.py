"""Bulk evaluation: score a whole dataset and read the metrics."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..annotations import DESTRUCTIVE, READ, WRITE
from ..panel import Panel


def register(server: MCPServer, panel: Panel) -> None:
    @server.tool(annotations=WRITE)
    async def bulk_upload(
        schema_id: int,
        csv_text: str,
        name: str = "",
        filename: str = "dataset.csv",
        state_column: str | None = None,
        label_column: str | None = None,
        model: str | None = None,
    ) -> Any:
        """Upload a CSV (or JSON) dataset, score every row, and return the metrics.

        The body is the dataset itself, not a path: pass the CSV as `csv_text`. One
        column holds the state (auto-detected when `state_column` is omitted) and an
        optional `label_column` supplies ground truth, which is what makes accuracy
        and ECE meaningful. The job runs synchronously — a large dataset blocks this
        call, so keep it to a few hundred rows (`capped by MAX_UPLOAD_MB`) or expect
        a timeout.
        """
        form = {"schema_id": str(schema_id)}
        if name:
            form["name"] = name
        if state_column:
            form["state_column"] = state_column
        if label_column:
            form["label_column"] = label_column
        if model:
            form["model"] = model

        return await panel.post(
            "/api/bulk/upload",
            tool="bulk_upload",
            data=form,
            files={"file": (filename, csv_text.encode("utf-8"), "text/csv")},
        )

    @server.tool(annotations=READ)
    async def bulk_jobs(limit: int = 50) -> Any:
        """Recent bulk jobs, newest first, with their status and `summary` metrics.
        Returns `{count, items}`."""
        return await panel.get_collection("/api/bulk/jobs", tool="bulk_jobs", params={"limit": limit})

    @server.tool(annotations=READ)
    async def bulk_job_get(job_id: int, include_items: bool = True, item_limit: int = 50) -> Any:
        """One bulk job with a preview of its rows: prediction, label, confidence and
        per-row errors. `summary` holds accuracy, mean confidence, ECE and the
        routing-band counts. Item preview is capped by `item_limit` — the full detail
        is at `/bulk/{id}` in the UI."""
        return await panel.get(
            f"/api/bulk/jobs/{job_id}",
            tool="bulk_job_get",
            params={"include_items": include_items, "item_limit": item_limit},
        )

    @server.tool(annotations=READ)
    async def bulk_job_items(job_id: int, offset: int = 0, limit: int = 100) -> Any:
        """Page through a job's rows. Returns `{count, items}`."""
        return await panel.get_collection(
            f"/api/bulk/jobs/{job_id}/items",
            tool="bulk_job_items",
            params={"offset": offset, "limit": limit},
        )

    @server.tool(annotations=WRITE)
    async def bulk_job_rerun(job_id: int) -> Any:
        """Re-score every row of a job — the step that reflects a calibration refit."""
        return await panel.post(f"/api/bulk/jobs/{job_id}/rerun", tool="bulk_job_rerun")

    @server.tool(annotations=DESTRUCTIVE)
    async def bulk_job_delete(job_id: int) -> Any:
        """Delete a job, its rows and their evaluations."""
        return await panel.delete(f"/api/bulk/jobs/{job_id}", tool="bulk_job_delete")
