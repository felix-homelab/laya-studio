"""The playground: score a state, explain routing, lint a question bundle."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..annotations import READ, WRITE
from ..panel import Panel


def register(server: MCPServer, panel: Panel) -> None:
    @server.tool(annotations=WRITE)
    async def evaluate(
        state: str | dict[str, Any],
        schema_key: str | None = None,
        schema_id: int | None = None,
        questions: dict[str, Any] | None = None,
        model: str | None = None,
        primary_question: str | None = None,
        apply_calibration: bool = True,
        persist: bool = True,
    ) -> Any:
        """Score one state with the real Laya engine — the main decision tool.

        `state` is free text, or an object (a parsed email, a ticket, arbitrary
        JSON). Give `schema_key` (e.g. "email-triage", see `schemas_list`) or an
        inline `questions` bundle: each question is `{type, instructions, criteria}`
        with type choice/score/noul. `primary_question` selects the question the
        confidence ladder gates on and defaults to the first one. `model` forces a
        checkpoint (english / multilingual / typed-decisions) instead of routing.

        Read the result carefully: `confidence` is Laya's entropy-based measure, not
        the top probability, and each question result carries its own `breakdown`,
        `band` and (when calibration applied) `raw_confidence`. `persist=False`
        scores without writing an evaluation row.
        """
        body: dict[str, Any] = {
            "state": state,
            "schema_key": schema_key,
            "schema_id": schema_id,
            "questions": questions,
            "model": model,
            "primary_question": primary_question,
            "apply_calibration": apply_calibration,
            "persist": persist,
        }
        return await panel.post(
            "/api/playground/evaluate",
            tool="evaluate",
            json={key: value for key, value in body.items() if value is not None},
        )

    @server.tool(annotations=READ)
    async def route(state: str | dict[str, Any]) -> Any:
        """Which checkpoint would serve this state, and why — without scoring it.

        Cheaper than `evaluate` and useful when a language switch is suspected of
        causing a reload: `routing.reason` says what the router decided on.
        """
        return await panel.post("/api/playground/route", tool="route", json={"state": state})

    @server.tool(annotations=READ)
    async def lint_questions(questions: dict[str, Any]) -> Any:
        """Lint a question bundle without running it.

        Returns `findings` with severity error/warning and an `ok` flag. The linter
        knows things the schema validator cannot: Laya *raises* rather than
        truncating when an option does not fit the head token budget, and one
        temperature bucket ships out of range above 11 options.
        """
        return await panel.post(
            "/api/playground/lint", tool="lint_questions", json={"questions": questions}
        )

    @server.tool(annotations=READ)
    async def evaluation_history(limit: int = 25, schema_id: int | None = None) -> Any:
        """Recent playground evaluations, newest first."""
        return await panel.get_collection(
            "/api/playground/history",
            tool="evaluation_history",
            params={"limit": limit, "schema_id": schema_id},
        )

    @server.tool(annotations=READ)
    async def evaluation_get(evaluation_id: int) -> Any:
        """One evaluation by id, including its per-question breakdown."""
        return await panel.get(
            f"/api/playground/evaluations/{evaluation_id}", tool="evaluation_get"
        )

    @server.tool(annotations=WRITE)
    async def evaluation_label(
        evaluation_id: int, label: str, label_source: str = "human"
    ) -> Any:
        """Attach ground truth to an evaluation.

        Labelled rows are what calibration is fitted on, so this is the tool that
        makes a dashboard's ECE move.
        """
        return await panel.post(
            f"/api/playground/evaluations/{evaluation_id}/label",
            tool="evaluation_label",
            json={"label": label, "label_source": label_source},
        )
