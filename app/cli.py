"""``laya-studio`` command line entry point.

    laya-studio check              verify the Laya runtime without downloading weights
    laya-studio db-check           verify the database connection and migrated schema
    laya-studio info               show the effective configuration and the live engine
    laya-studio predict            score one state end to end, as a smoke test
    laya-studio load-sample        evaluate a bundled labelled dataset through the real engine
    laya-studio mcp                serve the MCP tool surface (stdio or HTTP)
    laya-studio purge              drop every table (prompts unless --yes)
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import get_settings, redact_dsn
from .db import drop_schema, get_session_factory, reset_engine
from .inference import EngineUnavailable
from .logging_conf import configure_logging
from .services import decision_schemas


def _emit(payload: dict) -> None:
    """Machine-readable output on stdout; logs already go to stderr."""
    print(json.dumps(payload, indent=2, default=str))


def cmd_check(_args: argparse.Namespace) -> int:
    """Environment check. Imports the packages but downloads no weights."""
    from .inference.laya_engine import probe_runtime

    configure_logging(get_settings().log_level)
    report = probe_runtime()
    _emit(report)
    if not report["ok"]:
        print("\nLaya Studio has no fallback engine; fix the above before starting.", file=sys.stderr)
    return 0 if report["ok"] else 1


def cmd_info(_args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings.log_level)

    from .llm.service import get_provider

    provider = get_provider(settings)
    payload: dict = {
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "database_url": redact_dsn(settings.database_url),
        "llm": {
            "provider": settings.llm_provider,
            "enabled": settings.llm_enabled,
            "model": provider.describe().get("model") if provider else None,
        },
        "thresholds": {
            "auto": settings.auto_threshold,
            "verify": settings.verify_threshold,
        },
        "calibration": {
            "enabled": settings.calibration_enabled,
            "min_samples": settings.calibration_min_samples,
        },
    }

    try:
        from .inference import get_engine

        engine = get_engine()
        description = engine.describe()
        configuration = getattr(engine, "checkpoint_configuration", None)
        if callable(configuration):
            description["configuration"] = configuration()
        payload["engine"] = description
        status = 0
    except EngineUnavailable as exc:
        payload["engine"] = {"available": False, "error": str(exc)}
        status = 1

    _emit(payload)
    return status


def cmd_db_check(_args: argparse.Namespace) -> int:
    """Verify connectivity and that Prisma's migrations have been applied."""
    settings = get_settings()
    configure_logging(settings.log_level)

    # Prisma reads the same variable and cannot parse SQLAlchemy's `+driver` form.
    # Worth catching here rather than as a confusing failure inside `prisma migrate`.
    if "+" in settings.database_url.partition("://")[0]:
        print(
            "Warning: LAYA_STUDIO_DATABASE_URL contains a driver suffix "
            f"({settings.database_url.partition('://')[0]!r}).\n"
            "         `prisma migrate` cannot parse that. Use the plain "
            "postgresql:// form; the application adds the driver itself.\n",
            file=sys.stderr,
        )

    from sqlalchemy import func, select
    from sqlalchemy.exc import SQLAlchemyError

    from .db import SchemaNotMigrated, expected_tables, ping, verify_schema
    from .models import Evaluation

    payload: dict = {
        "database_url": redact_dsn(settings.database_url),
        "reachable": ping(),
        "migrated": False,
        "tables": {},
        "problems": [],
    }

    if not payload["reachable"]:
        payload["problems"].append(
            "The server did not answer. Check LAYA_STUDIO_DATABASE_URL and that "
            "PostgreSQL is running."
        )
        _emit(payload)
        return 1

    try:
        verify_schema()
        payload["migrated"] = True
    except SchemaNotMigrated as exc:
        payload["problems"].append(str(exc))
    except SQLAlchemyError as exc:
        # Reachable a moment ago, but the inspection itself failed (connection
        # dropped, missing privileges, ...). Still a report, not a traceback.
        payload["problems"].append(f"Could not inspect the schema: {exc}")

    if payload["migrated"]:
        from sqlalchemy import inspect

        from .db import get_engine

        present = set(inspect(get_engine()).get_table_names())
        payload["tables"] = {name: name in present for name in sorted(expected_tables())}

        session = get_session_factory()()
        try:
            payload["evaluations"] = int(
                session.scalar(select(func.count(Evaluation.id))) or 0
            )
            created = decision_schemas.seed_builtin_templates(session)
            payload["seeded_templates"] = [row.key for row in created]
        finally:
            session.close()

    _emit(payload)
    return 0 if payload["migrated"] else 1


def cmd_predict(args: argparse.Namespace) -> int:
    """Score one state against one schema — the smallest end-to-end check."""
    settings = get_settings()
    configure_logging(settings.log_level)

    from .inference import require_engine
    from .services import evaluation as evaluation_service

    try:
        require_engine()
    except EngineUnavailable as exc:
        print(f"Cannot evaluate: {exc}", file=sys.stderr)
        return 1

    session = get_session_factory()()
    try:
        decision_schemas.seed_builtin_templates(session)
        schema = decision_schemas.resolve_schema(session, args.schema)
        if schema is None:
            print(f"No schema {args.schema!r}. Known keys:", file=sys.stderr)
            for row in decision_schemas.list_schemas(session):
                print(f"  {row.key}", file=sys.stderr)
            return 1

        outcome = evaluation_service.run_evaluation(
            session,
            state={"text": args.text},
            schema=schema,
            model=args.model,
            primary_question=args.question,
            persist=not args.no_persist,
        )
    except EngineUnavailable as exc:
        print(f"Cannot evaluate: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()

    evaluation = outcome.evaluation
    _emit(
        {
            "schema": schema.key,
            "state_text": evaluation.state_text,
            "routing": evaluation.routing,
            "checkpoint": evaluation.engine_model,
            "latency_ms": round(evaluation.latency_ms, 2),
            "primary_question": evaluation.primary_question,
            "top_choice": evaluation.top_choice,
            "confidence": evaluation.confidence,
            "act_probability": next(
                (
                    entry.get("act_probability")
                    for entry in outcome.question_results
                    if entry["question"] == evaluation.primary_question
                ),
                None,
            ),
            "calibrated": evaluation.calibrated,
            "questions": [
                {
                    "question": entry["question"],
                    "type": entry["type"],
                    "choice": entry["choice"],
                    "level": entry["level"],
                    "score": entry["score"],
                    "noul": entry["noul"],
                    "confidence": entry["confidence"],
                    "band": entry["band"],
                    "top": entry["breakdown"][0] if entry["breakdown"] else None,
                }
                for entry in outcome.question_results
            ],
            "warnings": outcome.warnings,
        }
    )
    return 0


def cmd_load_sample(args: argparse.Namespace) -> int:
    """Evaluate a bundled labelled dataset. Real model, real numbers."""
    settings = get_settings()
    configure_logging(settings.log_level)

    from .inference import EngineUnavailable, require_engine
    from .samples import SampleError, available_samples, load_sample

    if args.list:
        _emit({"samples": available_samples()})
        return 0

    # The engine loads in the background in the web app; a CLI command has no
    # reason to stay asynchronous, so wait for it here.
    try:
        require_engine()
    except EngineUnavailable as exc:
        print(f"Cannot evaluate: {exc}", file=sys.stderr)
        return 1

    session = get_session_factory()()
    try:
        try:
            schema, job = load_sample(session, args.file)
        except (SampleError, EngineUnavailable) as exc:
            print(f"Cannot load the sample: {exc}", file=sys.stderr)
            return 1
        summary = dict(job.summary or {})
    finally:
        session.close()

    _emit(
        {
            "job_id": job.id,
            "schema": schema.key,
            "filename": job.filename,
            "rows": job.processed_rows,
            "failed_rows": job.failed_rows,
            "accuracy": summary.get("accuracy"),
            "ece": summary.get("ece"),
            "mean_confidence": summary.get("mean_confidence"),
            "band_counts": summary.get("band_counts"),
            "prediction_distribution": summary.get("prediction_distribution"),
            "next": (
                "Open /bulk to inspect the rows, /analytics for the reliability diagram, "
                "or POST /api/analytics/calibration/refit once you have enough labels."
            ),
        }
    )
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not args.yes:
        answer = input(f"Drop every table in {redact_dsn(settings.database_url)}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1
    reset_engine()
    drop_schema()
    print("All tables dropped. Re-apply the schema with: npx prisma migrate deploy")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="laya-studio", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="Verify the Laya runtime is importable and usable.")
    sub.add_parser("db-check", help="Verify the database connection and migrated schema.")
    sub.add_parser("info", help="Show effective configuration and the live engine.")

    predict = sub.add_parser("predict", help="Score one state end to end.")
    predict.add_argument("text", help="The state text to evaluate.")
    predict.add_argument("--schema", default="email-triage", help="Schema key or id.")
    predict.add_argument("--question", default=None, help="Question that drives the headline.")
    predict.add_argument(
        "--model",
        default=None,
        choices=["english", "multilingual", "typed-decisions"],
        help="Force a checkpoint; omit to let the router choose.",
    )
    predict.add_argument(
        "--no-persist", action="store_true", help="Do not record the evaluation."
    )

    sample = sub.add_parser(
        "load-sample",
        help="Evaluate a bundled labelled dataset with the real engine.",
    )
    sample.add_argument(
        "file",
        nargs="?",
        default="email-triage-sample.csv",
        help="Sample filename (see --list).",
    )
    sample.add_argument("--list", action="store_true", help="List bundled samples and exit.")

    purge = sub.add_parser("purge", help="Drop every table.")
    purge.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")

    mcp = sub.add_parser(
        "mcp",
        help="Serve the MCP tool surface for an agent (the panel serves it at /mcp too).",
    )
    mcp.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="stdio for a desktop client, http for a network endpoint.",
    )
    mcp.add_argument("--base-url", default=None, help="The panel to talk to.")
    mcp.add_argument("--host", default="127.0.0.1", help="http transport: bind address.")
    mcp.add_argument("--port", type=int, default=8765, help="http transport: bind port.")
    mcp.add_argument("--path", default="/mcp", help="http transport: the endpoint path.")

    return parser


def cmd_mcp(args: argparse.Namespace) -> int:
    """Serve the MCP tools.

    Delegates to :mod:`app.mcp.__main__` so that ``laya-studio mcp`` and
    ``python -m app.mcp`` are the same program — one implementation, two entry
    points, and no chance of the two drifting apart.
    """
    from .mcp.__main__ import main as mcp_main

    argv = ["--transport", args.transport, "--host", args.host,
            "--port", str(args.port), "--path", args.path]
    if args.base_url:
        argv += ["--base-url", args.base_url]
    return mcp_main(argv)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "check": cmd_check,
        "db-check": cmd_db_check,
        "info": cmd_info,
        "predict": cmd_predict,
        "load-sample": cmd_load_sample,
        "purge": cmd_purge,
        "mcp": cmd_mcp,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
