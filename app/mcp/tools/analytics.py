"""Analytics and the calibration monitor."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..annotations import READ, WRITE
from ..panel import Panel


def register(server: MCPServer, panel: Panel) -> None:
    @server.tool(annotations=READ)
    async def analytics_overview(hours: int = 168) -> Any:
        """The headline numbers over a window: evaluation volume, mean confidence,
        top choices, workflow route mix and how much of it was reviewed."""
        return await panel.get(
            "/api/analytics/overview", tool="analytics_overview", params={"hours": hours}
        )

    @server.tool(annotations=READ)
    async def analytics_dashboard(hours: int = 168, bucket: str = "hour") -> Any:
        """Everything the analytics page renders, in one call: distributions, volume
        over time, route mix and the reliability diagram. `bucket` is hour or day."""
        return await panel.get(
            "/api/analytics/dashboard",
            tool="analytics_dashboard",
            params={"hours": hours, "bucket": bucket},
        )

    @server.tool(annotations=READ)
    async def analytics_timeseries(hours: int = 168, bucket: str = "hour") -> Any:
        """Volume and confidence over time, bucketed by hour or day.
        Returns `{count, items}`."""
        return await panel.get_collection(
            "/api/analytics/timeseries",
            tool="analytics_timeseries",
            params={"hours": hours, "bucket": bucket},
        )

    @server.tool(annotations=READ)
    async def analytics_routes(hours: int = 168) -> Any:
        """How decisions were routed: auto vs verify vs review, and why."""
        return await panel.get(
            "/api/analytics/routes", tool="analytics_routes", params={"hours": hours}
        )

    @server.tool(annotations=READ)
    async def analytics_calibration(hours: int = 168) -> Any:
        """The calibration monitor: ECE, Brier, the reliability diagram, the
        ECE-per-day trend, and the checkpoints' own temperature table.

        ECE is the number that says whether the stated confidences are true, and
        therefore whether the thresholds mean anything. Above ~0.1 the ladder is
        gating on a number that does not predict correctness.
        """
        return await panel.get(
            "/api/analytics/calibration", tool="analytics_calibration", params={"hours": hours}
        )

    @server.tool(annotations=READ)
    async def calibration_report(
        schema_id: int | None = None, question: str | None = None
    ) -> Any:
        """An on-demand reliability report for one schema and/or question slice."""
        return await panel.get(
            "/api/analytics/calibration/report",
            tool="calibration_report",
            params={"schema_id": schema_id, "question": question},
        )

    @server.tool(annotations=WRITE)
    async def calibration_refit() -> Any:
        """Refit a temperature for every slice with enough labelled rows.

        Needs `calibration_min_samples` labelled evaluations per (schema, question)
        slice, so this returns "nothing to refit" until review has fed it. Re-run
        `bulk_job_rerun` afterwards to see the effect on a dataset's ECE."""
        return await panel.post("/api/analytics/calibration/refit", tool="calibration_refit")

    @server.tool(annotations=READ)
    async def analytics_thresholds() -> Any:
        """The live act-vs-escalate ladder and whether calibration is on."""
        return await panel.get("/api/analytics/thresholds", tool="analytics_thresholds")
