# Testing

```bash
make test                            # 237 tests, ~20s, starts its own Postgres
pytest --real-laya tests/            # adds 13 integration tests against real checkpoints
pytest -m 'not drift'                # skip the migration-drift check (~8s)
python -m pytest tests/ -x -q        # stop at the first failure
python -m pytest tests/ -k calibration
```

**237 tests in about 20 seconds**, plus 13 integration tests that load real
checkpoints and are deselected unless you ask for them.

## How that is possible

Laya is the only engine in the application. The suite still needs something
deterministic and fast to assert against, so it substitutes a **test double**
through `app.inference.set_engine` — the only substitution point, called from
nowhere in `app/`, and unreachable from configuration or HTTP.

Two doubles, for two jobs (`tests/doubles.py`):

* **`ScriptedEngine`** returns exactly what a test told it to. Use it whenever the
  test cares about a value: it removes any possibility of asserting the double's
  behaviour instead of the code under test.
* **`LexicalEngine`** is a deterministic keyword scorer, for tests that need
  plausible answers for *arbitrary* questions — bulk uploads, A/B comparisons,
  calibration runs — where scripting every question would be noise.

Both are installed by the `engine` fixture and removed afterwards, so a double can
never leak between tests. A third test (`test_doubles.py`) covers the doubles
themselves, because every other test depends on them staying deterministic and
returning a shape the normaliser accepts.

`tests/test_real_laya.py` is what keeps the adapter honest. It is the executable
form of [laya-integration.md](laya-integration.md) — every structural claim in
that document has an assertion here.

**A throwaway database.** `LAYA_STUDIO_DATABASE_URL` points at a file inside a
`tempfile.mkdtemp()` directory, and `LAYA_STUDIO_DATA_DIR` keeps uploads out of the
repository. `fresh_db` drops and recreates every table, then re-seeds the built-in
templates, before **each** test — so tests share nothing and can run in any order.

**A fake LLM provider.** The fallback is exercised through a `FakeProvider` that
returns canned JSON and records the prompts it received. No API key, no network,
no flakiness.

```python
def test_middle_band_auto_executes_when_the_fallback_agrees(client, triage_schema, fake_provider):
    workflow = make_workflow(client, triage_schema)
    probe = client.post(f"/api/workflows/{workflow['id']}/run", json={"text": "..."}).json()
    if probe["route"] != "verify":
        return
    fake_provider(json.dumps({"choice": probe["evaluation"]["top_choice"], "agrees": True}))
    body = client.post(f"/api/workflows/{workflow['id']}/run", json={"text": "..."}).json()
    assert body["effective_route"] == "auto"
```

## Layout

| File | Covers |
|---|---|
| `test_inference.py` | Normalisation, script routing, `validate_questions` |
| `test_doubles.py` | The test doubles themselves staying deterministic and well-shaped |
| `test_engine_lifecycle.py` | Engine states, and that the panel serves while Laya loads |
| `test_schema_drift.py` | Prisma's migrations and `app/models.py` describe the same schema |
| `test_real_laya.py` | **The real runtime** (deselected unless `--real-laya`) |
| `test_calibration.py` | ECE/MCE/Brier, reliability bins, temperature fitting, the fitting service |
| `test_api_playground.py` | Evaluate, route, lint, history, labelling |
| `test_api_schemas.py` | CRUD, revision minting, template protection, forking |
| `test_api_bulk.py` | CSV/JSON parsing, metrics, per-row failure isolation, lifecycle |
| `test_api_ab.py` | Revision comparison, flips, transitions, dataset sourcing |
| `test_api_workflows.py` | The ladder at every band, the fallback, actions, ingestion |
| `test_api_review.py` | Claim, resolve, label write-back, dedup, stats |
| `test_api_analytics.py` | Overview, time series, calibration monitor, refit |
| `test_actions.py` | SSRF guard, live webhook POST to a local server, templating, email |
| `test_security.py` | Ingestion auth scope, upload limits, injection-shaped input |
| `test_observability.py` | The JSONL store (rotation, retention, filters, error grouping), request and exception capture, the read-only API, token gating |
| `test_mcp_server.py` | The `/mcp` handshake, the tool surface and its annotations, tools against the real app, `db_query`'s guards, the token and enable switches |

## Fixtures

| Fixture | Scope | Gives you |
|---|---|---|
| `_settings` | session | Reloads settings from the test environment |
| `fresh_db` | function, autouse | Clean schema + seeded templates before every test |
| `client` | function | A `TestClient` with the app's lifespan run (so templates seed and the engine is reported) |
| `session` | function | A raw SQLAlchemy session, for service-level tests |
| `triage_schema` | function | The `email-triage` template as the API returns it |
| `sample_state` | function | A realistic email state dict |
| `engine` | function, autouse | Installs `LexicalEngine`, and removes any override afterwards |
| `scripted` | function | `scripted(**kwargs) -> ScriptedEngine`, to take over the answers for one test |
| `fake_provider` | function | `install(payload) -> FakeProvider`, wiring a canned LLM into `app.llm.service` |
| `settings_factory` | function | Build a modified `Settings` snapshot for config-dependent branches |

## What is tested, and why those things

The suite leans on behaviour that would be expensive to get wrong, rather than on
coverage for its own sake.

### The safety properties

These are the assertions that matter most, because they encode decisions a future
change could silently reverse.

```python
def test_middle_band_escalates_when_no_fallback_is_configured(client, triage_schema): ...
def test_unparseable_fallback_output_escalates_rather_than_guessing(client, triage_schema, fake_provider): ...
def test_viewer_invented_labels_are_not_accepted(client, triage_schema, fake_provider): ...
def test_private_targets_are_refused_by_default(...): ...        # SSRF, parametrised over 6 addresses
def test_the_token_guard_does_not_leak_to_other_routers(...): ...
def test_built_in_templates_cannot_be_deleted(...): ...
```

### The statistical properties

Calibration maths is tested against hand-computed values, not against itself:

```python
def test_perfectly_calibrated_confidences_have_no_gap():
    report = calibration_report([0.8] * 100, [True] * 80 + [False] * 20)
    assert report.ece == pytest.approx(0.0, abs=1e-9)

def test_overconfidence_shows_up_as_a_positive_gap():
    report = calibration_report([0.95] * 100, [True] * 50 + [False] * 50)
    assert report.ece == pytest.approx(0.45, abs=1e-9)
```

and the fitter is checked for *direction*, not for a magic number:

```python
temperature, _ = fit_temperature([0.95] * 200, [True] * 120 + [False] * 80)
assert temperature > 1.5                       # over-confident -> soften
assert abs(apply_temperature(0.95, temperature) - 0.6) < abs(0.95 - 0.6)
```

### The conditions that are easy to forget

* `fit_temperature` on all-correct outcomes returns `1.0` **and says why**, rather
  than inventing a number;
* `noul` questions have an empty `breakdown` — a binary head has no distribution;
* metric keys in a bulk `summary` are present-but-null rather than absent;
* an `email` ingest payload does not leak the Pydantic alias `from_` into the state;
* a rerun of a bulk job creates exactly `n` new evaluations, not `2n`.

### The surfaces that cannot be asserted from the outside

Three things about the observability and MCP work needed tests that step outside the
usual request/assert shape, because the failure modes are not status codes:

* **A request that raises.** The suite installs a route that raises, calls it with
  `raise_server_exceptions=False`, and then reads the *store* to prove the traceback
  and fingerprint were written. This caught a real bug: the SQL guard was validating
  against a copy with string literals stripped, and executing that copy.
* **The process-level hooks.** `install_exception_hooks` is exercised by invoking
  `threading.excepthook` directly with a synthetic `ExceptHookArgs`, which is how
  "the engine loader thread died" is proven to reach the store.
* **A single-use session manager.** Every `TestClient` enters the app's lifespan, so
  the MCP transport has to survive being started more than once per process. A test
  that only ever made one client would have passed while the suite failed.

The MCP tests also use `base_url="http://127.0.0.1:8000"` rather than the default
`http://testserver`: the endpoint keeps DNS-rebinding protection on, and a test that
used a Host the panel has not been told about would be asserting the 421, not the
tool.

### The JavaScript is not covered at all

Every assertion in this file is made against the HTTP API. The panel's editors —
the dropdowns, the question cards, the workflow block script — are plain DOM code
in `static/js/`, and nothing in the suite executes them. A pytest run cannot see
them, and the API tests stay green while an editor renders the wrong thing.

That is a real gap, and it hides a specific failure mode: **a broken editor renders
successfully**. A `score` question whose rubric levels were stringified into
`"[object Object]"` produced a 200 from every endpoint, linted clean, and would
have been *saved* as those literal strings. The suite would not have said a word.

So a change under `app/static/` needs a pass in a browser, and any assertion worth
keeping has to be made in the browser's own terms. The practical version of that is
a jsdom harness that loads the real templates and the real `static/js` files with
the HTTP layer stubbed, then drives them by clicking and typing — asserting the
*values* in the DOM and the payloads actually sent, not just that elements exist.
It is deliberately not wired into this suite: the panel ships without a Node
toolchain ([ADR 0007](adr/0007-no-build-step-frontend.md)), and adding one for
tests alone is a trade this project has not made.

### Property-style assertions where a value would be brittle

Middle-band tests probe first and skip if the lexically-random input did not land in
the band:

```python
probe = client.post(f"/api/workflows/{workflow['id']}/run", json={"text": "..."}).json()
if probe["route"] != "verify":
    return
```

This is deliberate. Forcing the double's confidence to land in the band would make
the test pass while asserting nothing about the ladder. Where a test genuinely needs
a specific band, it uses `scripted` and says so.

## Writing a new test

```python
def test_my_new_behaviour(client, triage_schema):
    created = client.post(
        "/api/playground/evaluate",
        json={"state": {"body": "we were billed twice"}, "schema_id": triage_schema["id"]},
    )
    assert created.status_code == 200
```

Use the `client` fixture (it runs the app's lifespan, so startup behaviour is
covered) and `triage_schema` rather than hard-coding ids. `fresh_db` has already
run, so the four templates exist and nothing else does.

For service-level work, take `session`:

```python
def test_something_at_the_service_layer(session, triage_schema):
    from app.services import decision_schemas, evaluation

    schema = decision_schemas.get_schema(session, triage_schema["id"])
    outcome = evaluation.run_evaluation(session, state={"text": "..."}, schema=schema)
    assert outcome.evaluation.top_choice
```

For the fallback, take `fake_provider`:

```python
def test_with_a_provider(client, triage_schema, fake_provider):
    fake_provider('{"choice": "billing", "agrees": true, "rationale": "obvious"}')
```

To control exactly what the engine returns, take `scripted`:

```python
def test_a_choice_flip(client, triage_schema, scripted):
    scripted(answers={"department": {"type": "choice", "choice": "sales",
                                     "probabilities": {"billing": 0.2, "sales": 0.8},
                                     "confidence": 0.8, "action": {"act_probability": 0.9}}})
```

### Rules of thumb

* **Assert behaviour, not implementation.** `assert body["effective_route"] == "auto"`
  survives refactoring; `assert service._compute_route(...) == "auto"` does not.
* **Script the value you care about.** If a test asserts a specific label, use
  `scripted` rather than hoping the lexical double picks it. Asserting the double's
  behaviour is a test that passes for the wrong reason.
* **Do not assert on `LexicalEngine`'s exact probabilities.** They are a
  convenience, not a model. Assert direction, ordering, and sum-to-one.
* **One behaviour per test.** The names are sentences; keep them true.
* **Prefer the API.** Internal call shape changes; the HTTP contract is the thing
  you promised.

### Adding to the real-runtime suite

Anything structural about Laya — a result key, a routing field, a token budget
behaviour — belongs in `tests/test_real_laya.py`, not in the fast suite. It costs a
checkpoint load, so use a module-scoped `engine` fixture and keep assertions about
shape rather than exact values, which drift with the checkpoint.

```bash
pytest --real-laya tests/test_real_laya.py -v -k temperature
```

## Linting

```bash
make lint      # ruff check app tests
make fmt       # ruff check --fix app tests
```

`pyproject.toml` configures ruff with `E, F, I, UP, B` at a 100-character line
length. `make check` runs lint then tests — worth wiring into CI as-is.

## The schema-drift check

The schema is defined twice — `prisma/schema.prisma` for DDL, `app/models.py` for
queries — so something has to check they agree. `test_schema_drift.py` creates a
scratch database, runs `prisma migrate deploy` against it, and compares the result
column by column: tables, types, nullability, primary keys, unique constraints,
foreign keys (including `ON DELETE`) and index names.

```
FAILED tests/test_schema_drift.py::test_prisma_migrations_match_the_sqlalchemy_models
  The Prisma migrations and app/models.py have drifted apart.
  Deciding which is wrong, fix it, and regenerate the migration with
  `npx prisma migrate dev --name <change>` if the schema itself changed:

    evaluations.confidence: type/nullability differ —
      migrations=('DOUBLE PRECISION', True) models=('FLOAT', True)
```

It is the slowest test in the suite (~8s, because it shells out to Node) and it is
worth every second: without it, "Prisma owns the schema" means nobody knows what the
schema is. Skip it with `-m 'not drift'` when iterating.

It skips itself with a clear message if `npx` or `node_modules/prisma` is missing.

## Test data

`tests/test_api_bulk.py` uses an inline CSV:

```python
CSV_WITH_LABELS = (
    "subject,body,truth\n"
    "Duplicate charge,We were billed twice and need a refund,billing\n"
    "API 500,Our integration is broken and production is blocked,technical\n"
    ...
)
```

Prefer inline fixtures over files on disk. A test's input should be visible next to
its assertion.

For a larger corpus, `datasets/email-triage-sample.csv` holds 40 labelled rows and
`laya-studio load-sample` evaluates them for real. It is used by the CLI and by
demos, not by the test suite — the suite stays small and fast on purpose.

## Continuous integration

There is no CI configuration in the repository yet
([ROADMAP.md](ROADMAP.md#11-ci)). The whole check is:

```bash
pip install -r requirements.txt pytest ruff
npm ci
make check
```

Which is what a CI job should run. Nothing needs a GPU, a model download or a
secret — the doubles keep the fast suite hermetic, and the real-checkpoint suite is
opt-in.

**A database is needed.** Either let the suite start its own container (a
Docker-capable runner), or provide one:

```bash
export LAYA_STUDIO_TEST_DATABASE_URL='postgresql://user:pass@localhost:5432/laya_ci'
make test
```

Setting that variable skips Docker entirely, which is usually faster and always less
fragile in CI.

A CI job that *wants* to cover the real path can add a cache step and run
`pytest --real-laya`. It needs ~1 GB of disk and a couple of minutes; no GPU is
required, CPU inference works, it is just slower.
