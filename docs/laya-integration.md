# Laya integration

How Laya Studio talks to Laya, and what the runtime actually does — verified by
running it, not by reading the model card.

**Verified against `laya 0.3.5`, `torch 2.14`, `transformers 5.17`.** The
assertions in `tests/test_real_laya.py` are the executable form of everything on
this page:

```bash
pytest --real-laya tests/test_real_laya.py
```

---

## What Laya is

An open-source (Apache-2.0) **non-autoregressive decision model**. Instead of
generating text token by token, it scores typed questions against a state — text,
an email, an arbitrary JSON dict — and returns structured answers with
probabilities in one forward pass.

Because it never generates, it cannot produce malformed JSON or invent an enum
value outside your taxonomy. `usage.output_tokens` is always `0`.

### Primitives

| Primitive | Returns | Use for |
|---|---|---|
| `choice` | winning label, per-option probabilities, confidence | routing, intent, categorisation |
| `score` | expected level on an ordinal rubric, per-level distribution | urgency, frustration, severity |
| `noul` | calibrated `P(true)` | phishing, spam, jailbreak, churn — binary guardrails |

### Checkpoints

Three checkpoints, selected by the router (or forced with `model=`):

| Checkpoint | Encoder | Params | `max_len` | `head_max_len` | Purpose |
|---|---|---|---|---|---|
| `english` | ModernBERT-large | 421M | 512 | 192 | English |
| `multilingual` | mmBERT-base | 322M | 1024 | 256 | 100+ languages |
| `typed-decisions` | ModernBERT-large | 421M | 1024 | — | schemas that need the longer context |

The `max_len` / `head_max_len` columns are read from each checkpoint's
`rl_agent_config.json`, not from documentation. They matter — see
[Token budgets](#token-budgets).

---

## The API, as it actually is

```python
Router(models=None, device=None, token=None, max_loaded=1, default="english",
       auto_task_detection=False, standalone_repos=False, preload=False)

Router.predict(state, questions, model=None, task=None, lang=None) -> dict
Router.route(state, questions=None, model=None, task=None, lang=None) -> RouteDecision
Router.load(name) -> Agent          # .cfg, .device, .model, .tok
Router.preload(names=None)          # raises max_loaded to fit whatever it loads
Router.attach(name, agent)
Router.unload(name=None)
```

`predict` routes, loads the chosen checkpoint, and returns:

```python
{
  "model": "laya-rl-agent",              # internal agent class name — see below
  "answers": {
    "department": {
      "type": "choice",
      "choice": "billing",
      "probabilities": {"billing": 0.9432, "technical": 0.016, "sales": 0.0259, "other": 0.015},
      "confidence": 0.7989,
      "action": {"act_probability": 1.0},
    },
    "urgency": {
      "type": "score",
      "score": 1.6439,
      "legend": {"0": "no time pressure", "1": "soon", "2": "blocking or a deadline"},
      "probabilities": {"0": 0.087, "1": 0.1822, "2": 0.7308},
      "confidence": 0.3157,
      "action": {"act_probability": 1.0},
    },
    "churn": {
      "type": "noul",
      "noul": 0.822,
      "confidence": 0.822,
      "action": {"act_probability": 1.0},
    },
  },
  "usage": {"input_tokens": 219, "output_tokens": 0},
  "routing": {"model": "english", "repo": "convaiinnovations/laya", "reason": "English Latin text",
              "detection": {...}, "workflow": None},
}
```

*(Real output, trimmed.)*

### Four details that are easy to get wrong

**1. `confidence` is not the top probability.** For the `department` answer above,
the top probability is `0.9432` and the confidence is `0.7989`. Laya computes
confidence as a **normalised Shannon entropy**:

```
confidence = 1 - H(p) / log(k)
```

`k` is the option count. That is the number the confidence ladder gates on, so
treating it as "how likely the top label is" would mis-set every threshold by a
wide margin — and the gap widens with `k`. On a 2-way `noul` the two coincide; on
a 4-way `choice` they differ by ~15 points; on a 10-way they differ much more.

**2. `routing["model"]` names the checkpoint; the top-level `model` key does not.**
`result["model"]` is `"laya-rl-agent"` — the internal class. Provenance uses
`routing["model"]` (`english` / `multilingual` / `typed-decisions`), which is what
`Evaluation.engine_model` stores.

**3. `score` answers carry a separate `legend`.** Probabilities are keyed by index
(`"0"`, `"1"`, `"2"`), which is meaningless without the level text. Laya Studio
maps them back through the legend, so a breakdown shows the rubric the author
wrote rather than bare numbers. The `legend` wins over the stored schema if the
two ever disagree, because it is what the model actually rendered.

**4. There is a trained `action.act_probability`.** A separate decision head emits
its own view on whether to act automatically or escalate. Laya Studio surfaces it
on every question card (the violet `act …` chip) but does **not** route on it: the
brief specifies a confidence ladder, and `confidence` is the value the model card
tells you to gate on.

Worth knowing before you build on it: in every case tested — clear routing,
nonsense input, a near-uniform distribution — it returned `1.0`. It is recorded and
displayed, but on the inputs tested it did not discriminate.

---

## Routing

`Router.route` detects script and language *before* the forward pass and picks the
checkpoint. This is a correctness requirement, not a nicety: an English-vocabulary
checkpoint fails catastrophically on non-Latin input while staying **confident**
(Khmer 0.000 accuracy at 0.952 confidence in the published sweep). Confidence
gating gives no warning when the model simply cannot read the input.

Observed routing payloads:

```json
// English
{"model": "english", "repo": "convaiinnovations/laya", "reason": "English Latin text",
 "detection": {"script": "latin", "script_profile": {"latin": 1.0}, "language": "en",
               "is_english": true, "language_undecided": false,
               "diacritic_rate": 0.0, "non_latin_fraction": 0.0},
 "workflow": null}

// Non-Latin
{"model": "multilingual", "reason": "non-Latin script (kana, 100% of letters); ..."}
```

Laya Studio's `inference/routing.py` implements the same detection locally. It is
used for two things: the playground's `POST /api/playground/route` probe, which
answers without a forward pass, and as a fallback if `Router.route` is ever
unavailable. Both detectors agreed on every input tested; the local one is what
the UI renders for the script label.

---

## Checkpoint calibration is already applied

This is the most important thing to understand before interpreting any number.

Laya ships **temperature scaling fitted per `(question type, option-count bucket)`**
and applies it *inside* `Agent.system_one`, before it reports probabilities. The
shipped English values:

| Bucket | Temperature | |
|---|---|---|
| `choice:2` | 1.9064 | softening |
| `choice:3-5` | 1.7602 | softening |
| `choice:6-10` | 1.0000 | identity |
| `choice:11+` | **0.1006** | ⚠️ below Laya's clamp floor |
| `score:3-5` | 1.2514 | softening |
| `noul:2` | 1.9834 | softening |

Per-type fallbacks: `choice` 1.6369, `score` 1.2514, `noul` 1.9834.

The multilingual checkpoint is different: `temperature = [1.0, 1.0, 1.0]` and an
empty `temperature_by_options`. **It ships no refit at all** — its probabilities
are raw logits scaled by 1.0.

Two consequences:

* **Every temperature except one is > 1**, which is the model card's
  "over-confident, needs refitting" story — and Laya has already done the refit
  for you at inference time. You are not looking at raw logits.
* **`choice:11+` is out of range.** 0.1006 is below Laya's clamp floor of 0.5, so
  the runtime clamps it and warns:

  ```
  RuntimeWarning: laya: this checkpoint ships temperatures outside [0.5, 5] which
  would distort confidence; clamping choice:11+=0.1006. Treat confidence from the
  affected buckets as uncalibrated.
  ```

  An 11-option `choice` question therefore produces a confidence **nobody has
  calibrated**, and the ladder gates on exactly that number. This is why the
  schema linter warns at 11 options rather than at some hand-waved accuracy cliff.

You can see all of this in the running panel: **Analytics → Checkpoint
calibration**, or `GET /api/health`. The read-out comes from
`LayaEngine.checkpoint_configuration()`.

### What Laya Studio's calibration adds

Because Laya already corrects the *distribution*, Laya Studio's calibration is a
**second-stage recalibration of the reported confidence**: it measures whether
`confidence` predicts correctness on *your* data, and fits a two-parameter logistic
map on top.

The two are complementary, not redundant:

| | What it fits | What it fixes |
|---|---|---|
| Laya (in-checkpoint) | logits → distribution | over-/under-confident *option probabilities*, per type and option bucket |
| Laya Studio (second stage) | `confidence` → `P(correct)` | whether the entropy-based confidence predicts correctness on your traffic |

They are fitted on different inputs *and* different evidence: Laya's on its training
distribution, Laya Studio's on your labelled rows. And they are different shapes —
Laya's is a temperature on the logits, Laya Studio's is a logistic map with an
intercept, because a single temperature provably cannot correct an entropy-based
confidence that sits below 0.5 while the model is mostly right. Measured on the
bundled sample: **ECE 0.446 → 0.054**. See
[analytics-and-calibration.md](analytics-and-calibration.md#the-calibration-map).

---

## Token budgets

`head_max_len` caps the tokens spent rendering a question's options; `max_len`
caps the whole sequence. Laya builds:

```
[CLS] <type> instructions [SEP] opt0 opt1 … [SEP] state [SEP]
```

From `laya.common.build_sequence`:

1. Each option is rendered and capped at **48 tokens**.
2. If the options do not fit in `head_max_len`, *all* of them are truncated to
   a shared per-option budget of `max(4, (head_max_len - 16) / n_options)`.
3. The instructions are then truncated to whatever budget remains.
4. The finished sequence is sliced to `max_len`.
5. Markers past `max_len` are dropped — and if the surviving marker count no
   longer matches the option count, Laya raises rather than scoring a partial
   option set.

**So raising `head_max_len` can make things worse.** Measured on laya 0.3.5, 40
long options against `max_len=512`:

| `head_max_len` | Result |
|---|---|
| 192 (checkpoint default) | works |
| 700 | `ValueError: question 'q' options exceed head_max_len=700` |

At 192 the options get truncated to fit and all 40 survive inside `max_len`. At
700 they are given room they do not have, the sequence overruns, and the trailing
markers are cut.

Laya Studio therefore **refuses a `head_max_len` that is not below `max_len`**,
logging the reason instead of breaking a schema that was working:

```
LAYA_STUDIO_LAYA_MAX_LEN=1024
LAYA_STUDIO_LAYA_HEAD_MAX_LEN=512
```

Raise both or neither. The `QuestionTooLarge` error carries this remedy, and the
API returns it as a 422 with a `remedy` field.

The real fix for a genuinely large taxonomy is shortlisting rather than a bigger
budget — `laya.predict_shortlist` ranks options by embedding similarity and scores
only the top `k`. It is on the [roadmap](ROADMAP.md#9-predict_shortlist-for-high-cardinality-choice).

---

## Latency

Measured on an Apple M-series laptop over MPS, `english` checkpoint, warm:

| Questions in one call | Median | Per question |
|---|---|---|
| 1 | 34 ms | 34 ms |
| 2 | 47 ms | 23 ms |
| 4 | 77 ms | 19 ms |
| 8 | 139 ms | 17 ms |
| 16 | 270 ms | 17 ms |
| 32 | 534 ms | 17 ms |

Read that honestly: **all questions share one forward pass, so the marginal cost
is lower than the first — but it is not free.** There is a fixed ~17 ms per call
plus ~17 ms per question. A four-question schema costs ~2.3× a one-question
schema, not 1× and not 4×.

The published T4 figure is ~33 ms for one question and ~72 ms for ten (7.2 ms/q).
This machine lands at ~17 ms/q, consistent with a slower accelerator.

**First-inference cost is separate and much larger.** The first real prediction
after a cold load measured **~2000 ms** while torch set up kernels. Laya Studio
runs one throwaway forward pass at startup (`_prime_inference`) so the operator's
first click does not pay it.

**Preloading matters for a panel.** `Router(max_loaded=1)` evicts on every load,
so a process that alternates languages reloads a checkpoint mid-request — seconds,
not milliseconds. `LAYA_STUDIO_LAYA_PRELOAD=true` (the default) keeps `english` and
`multilingual` resident, at roughly 2.4 GB of RAM. `typed-decisions` is left out
because only an explicit `model=` request can reach it.

---

## Documented limits

Laya's own published caveats, repeated because they determine what you build on
top of it.

### It is not a zero-shot engine

> *"Laya is a fast base to specialise, not a zero-shot decision engine."* — model card

The base checkpoint scores ~0.362 on the typed-decisions benchmark, against a
0.318 random baseline and a 0.461 majority-class baseline. The widely-quoted 0.766
figure comes from a checkpoint **fine-tuned on that benchmark's own training
split**. Expect to fine-tune per domain, and evaluate on your own data — which is
what the bulk and A/B modules are for.

Fine-tuned per-primitive accuracy is uneven: `noul` 0.857, `choice` 0.733, `score`
0.723. Ordinal `score` remains the weakest surface.

### Ordinal `score` is coarse

`score` returns an expected level plus a per-level distribution. On a 3-level
rubric with a genuinely ambiguous state, the distribution flattens: an observed
`confidence` of 0.3157 on `urgency` above is Laya saying "I put most of the mass on
level 2, but this rubric does not separate cleanly". That is information, not a
bug — but a `score` question is a bad choice of `decision_question` for a workflow
ladder.

### Thresholds need calibrating against your data

The brief's 0.90 / 0.60 thresholds are the default. Because confidence is
entropy-based, a 4-way `choice` rarely reaches 0.90 even when the top option is
overwhelming: `billing` at 0.9432 top-probability reported confidence 0.7989. In a
five-row bulk run against real Laya, the highest confidence observed was 0.7547
and **no row reached the auto band**.

That is not a defect in the ladder — it is what the ladder is *for*. Run a labelled
batch, look at the ECE and the confidence distribution, and set the thresholds from
that. See [analytics-and-calibration.md](analytics-and-calibration.md).

---

## Operating it

### Requirements

Laya needs Python 3.10+. The container image pins 3.12.

```bash
pip install -r requirements.txt        # pulls laya, torch, transformers
```

### Verifying the environment

```bash
python -m app.cli check    # imports the packages, reports the device and cache; downloads nothing
python -m app.cli info     # the live engine: device, loaded checkpoints, temperature table
```

`check` exits non-zero when something is missing, which makes it a serviceable
container entrypoint guard.

### Startup is asynchronous

The engine is built on a background thread so the panel is reachable while the
checkpoints load. Measured with the real runtime: port open at **0.6s**, engine
ready at **42s** (warm cache). Decisions return 503 with a `Retry-After` until
ready; pages render throughout and say so.

`laya-studio check` verifies the environment without loading anything, and
`python -m app.cli predict` blocks until the engine is ready — the CLI waits, the
web app does not. See [ADR 0011](adr/0011-background-engine-startup.md).

### Settings

| Variable | Default | Effect |
|---|---|---|
| `LAYA_STUDIO_LAYA_PRELOAD` | `true` | Keep `english` + `multilingual` resident, and prime inference at startup. Costs ~2.4 GB RAM and ~42s of background loading; removes per-request reloads and the ~2s first-inference warm-up. |
| `LAYA_STUDIO_LAYA_DEVICE` | `auto` | `auto` \| `cpu` \| `cuda` \| `mps`. Passed to `Router` only if it accepts the keyword. |
| `LAYA_STUDIO_LAYA_CACHE_DIR` | `~/.cache/huggingface` | Sets `HF_HOME` / `HF_HUB_CACHE`. Point it at a mounted volume in Docker. |
| `LAYA_STUDIO_LAYA_TOKEN` | — | HF token for a gated or private mirror. Falls back to `HF_TOKEN`. |
| `LAYA_STUDIO_LAYA_MAX_LOADED` | Laya's default (1) | How many checkpoints stay in memory. |
| `LAYA_STUDIO_LAYA_MAX_LEN` | checkpoint's own | Sequence budget. Raise with `HEAD_MAX_LEN`, never instead of it. |
| `LAYA_STUDIO_LAYA_HEAD_MAX_LEN` | checkpoint's own | Option + instruction budget. Refused if >= `max_len`. |
| `LAYA_STUDIO_LAYA_OFFLINE` | `false` | Sets `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE`. |

### Pre-populating the cache

```bash
huggingface-cli download convaiinnovations/laya --local-dir ./models/laya
HF_CACHE=./models docker compose up --build
```

With `LAYA_STUDIO_LAYA_OFFLINE=true` the runtime never attempts a network call.

### What the adapter does defensively

`LayaEngine` treats the runtime as a dependency that can change under it:

* **Constructor signatures are probed** with `inspect.signature`, so a keyword
  added or removed in a release cannot break boot.
* **`predict` falls back** to a two-argument call if the `model=` keyword is
  absent.
* **`route` falls back** to the local script detector if `Router.route` errors.
* **`head_max_len` is applied by wrapping `Router.load`**, so lazily-loaded agents
  get it too, and the change is refused when it would break the schema.
* **Inference is not serialised.** Laya's `Router` guards model lifecycle with its
  own lock and documents that `Agent.system_one` is deliberately left outside it so
  concurrent predictions share a checkpoint. Each call builds fresh tensors and
  holds a local reference, so an LRU eviction mid-flight cannot pull the model out
  from under it.

---

## Sources

* PyPI — <https://pypi.org/project/laya/>
* GitHub — <https://github.com/NandhaKishorM/laya>
* Hugging Face — <https://huggingface.co/convaiinnovations/laya>
* Independent head-to-head benchmark — <https://github.com/instax-dutta/sysone-bench>

> The Laya-vs-Jev comparisons are self-reported by Convai, mixing Laya's own
> measurements with third-party published figures for Jev. Treat them as
> directional. The measurements on this page are not from those sources; they were
> taken from the installed runtime and are reproducible with
> `pytest --real-laya`.
