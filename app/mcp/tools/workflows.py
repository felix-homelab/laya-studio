"""Workflows: triggers, the confidence ladder, and what each run did."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..annotations import DESTRUCTIVE, READ, WRITE
from ..panel import Panel


def register(server: MCPServer, panel: Panel) -> None:
    @server.tool(annotations=READ)
    async def workflows_list(enabled_only: bool = False) -> Any:
        """Every workflow: its trigger, schema, decision question and its own
        auto/verify thresholds. The thresholds default to the panel's, and they are
        per workflow on purpose — they belong to the measured ECE of that domain.
        Returns `{count, items}`."""
        return await panel.get_collection(
            "/api/workflows", tool="workflows_list", params={"enabled_only": enabled_only}
        )

    @server.tool(annotations=READ)
    async def workflow_get(workflow_id: int) -> Any:
        """One workflow definition, including its `steps`."""
        return await panel.get(f"/api/workflows/{workflow_id}", tool="workflow_get")

    @server.tool(annotations=WRITE)
    async def workflow_create(
        name: str,
        schema_id: int | None = None,
        description: str = "",
        trigger_type: str = "api",
        trigger_config: dict[str, Any] | None = None,
        steps: list[dict[str, Any]] | None = None,
        decision_question: str | None = None,
        auto_threshold: float | None = None,
        verify_threshold: float | None = None,
        enabled: bool = True,
        key: str | None = None,
    ) -> Any:
        """Create a workflow. `trigger_type` is api / webhook / email / manual. Each
        step is `{on: auto|verify|review|always, kind: webhook|email|review|noop,
        config: {...}}`. Leave the thresholds unset to inherit the panel's."""
        return await panel.post(
            "/api/workflows",
            tool="workflow_create",
            json={
                "name": name,
                "description": description,
                "schema_id": schema_id,
                "trigger_type": trigger_type,
                "trigger_config": trigger_config or {},
                "steps": steps or [],
                "decision_question": decision_question,
                "auto_threshold": auto_threshold,
                "verify_threshold": verify_threshold,
                "enabled": enabled,
                "key": key,
            },
        )

    @server.tool(annotations=WRITE)
    async def workflow_update(
        workflow_id: int,
        name: str | None = None,
        description: str | None = None,
        enabled: bool | None = None,
        trigger_type: str | None = None,
        trigger_config: dict[str, Any] | None = None,
        steps: list[dict[str, Any]] | None = None,
        schema_id: int | None = None,
        decision_question: str | None = None,
        auto_threshold: float | None = None,
        verify_threshold: float | None = None,
    ) -> Any:
        """Update a workflow. Only the fields you pass are changed."""
        return await panel.put(
            f"/api/workflows/{workflow_id}",
            tool="workflow_update",
            json={
                "name": name,
                "description": description,
                "enabled": enabled,
                "trigger_type": trigger_type,
                "trigger_config": trigger_config,
                "steps": steps,
                "schema_id": schema_id,
                "decision_question": decision_question,
                "auto_threshold": auto_threshold,
                "verify_threshold": verify_threshold,
            },
        )

    @server.tool(annotations=DESTRUCTIVE)
    async def workflow_delete(workflow_id: int) -> Any:
        """Delete a workflow. Its runs and review items stay behind."""
        return await panel.delete(f"/api/workflows/{workflow_id}", tool="workflow_delete")

    @server.tool(annotations=DESTRUCTIVE)
    async def workflow_run(
        workflow_id: int,
        state: str | dict[str, Any] | None = None,
        text: str | None = None,
        dry_run: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Fire a workflow manually and return the whole decision: the route, the
        verification result, every action attempted and the evaluation behind it.

        Pass `state` (text or object) or `text`. `dry_run=True` evaluates and shows
        the route without executing any action — use it before running something
        that posts webhooks or emails. This is destructive by annotation because a
        real run has side effects outside the panel.
        """
        return await panel.post(
            f"/api/workflows/{workflow_id}/run",
            tool="workflow_run",
            json={
                "state": state,
                "text": text,
                "dry_run": dry_run,
                "metadata": metadata or {},
            },
        )

    @server.tool(annotations=READ)
    async def workflow_runs(
        workflow_id: int | None = None, route: str | None = None, limit: int = 100
    ) -> Any:
        """Run history, newest first, optionally for one workflow or one ladder route
        (auto / verify / review). Returns `{count, items}`."""
        if workflow_id is None:
            return await panel.get_collection(
                "/api/workflows/runs", tool="workflow_runs", params={"route": route, "limit": limit}
            )
        return await panel.get(
            f"/api/workflows/{workflow_id}/runs",
            tool="workflow_runs",
            params={"route": route, "limit": limit},
        )

    @server.tool(annotations=READ)
    async def workflow_run_get(run_id: int) -> Any:
        """One run with its action logs — what was attempted, where it went and what
        came back. This is where a failed webhook action shows its target and error."""
        return await panel.get(f"/api/workflows/runs/{run_id}", tool="workflow_run_get")

    @server.tool(annotations=READ)
    async def workflow_stats() -> Any:
        """Volume, route mix and failure counts across all workflows."""
        return await panel.get("/api/workflows/stats", tool="workflow_stats")
