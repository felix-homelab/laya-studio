"""The ingestion doors — the same paths a webhook or an email worker would use."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer

from ..annotations import DESTRUCTIVE
from ..panel import Panel


def register(server: MCPServer, panel: Panel) -> None:
    @server.tool(annotations=DESTRUCTIVE)
    async def ingest(
        workflow_key: str,
        state: str | dict[str, Any] | None = None,
        text: str | None = None,
        dry_run: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Send a state through a workflow exactly as an API caller would.

        `workflow_key` is the workflow's key, not its id. This executes the ladder
        and the workflow's actions for real; `dry_run=True` evaluates and returns the
        route without a single action, which is the right first call against an
        unfamiliar workflow.

        Pass the state as `state` (a string or an object), or pass `text` as
        shorthand for `{"text": ...}`. When both are given, `state` wins and `text`
        is ignored.
        """
        return await panel.post(
            f"/api/ingest/{quote(workflow_key, safe='')}",
            tool="ingest",
            json={
                "state": state,
                "text": text,
                "dry_run": dry_run,
                "metadata": metadata or {},
            },
        )

    @server.tool(annotations=DESTRUCTIVE)
    async def ingest_text(workflow_key: str, text: str) -> Any:
        """The plain-text door: a raw body, no JSON envelope, for dumb senders."""
        return await panel.post(
            f"/api/ingest/{quote(workflow_key, safe='')}/text",
            tool="ingest_text",
            data=text,
        )

    @server.tool(annotations=DESTRUCTIVE)
    async def ingest_email(
        workflow_key: str,
        body: str,
        from_: str | None = None,
        to: str | None = None,
        subject: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Ingest a parsed inbound email: `from`, `to`, `subject` and `body` become
        the state the schema's questions are asked about."""
        return await panel.post(
            f"/api/ingest/email/{quote(workflow_key, safe='')}",
            tool="ingest_email",
            json={
                "from": from_,
                "to": to,
                "subject": subject,
                "body": body,
                "metadata": metadata or {},
            },
        )
