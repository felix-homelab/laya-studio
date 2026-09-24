# 0003 — An unverifiable middle band escalates rather than auto-executes

**Status:** Accepted

## Context

The hybrid fallback design: confidence ≥ 0.90 auto-executes, 0.60–0.90 gets
adjudicated by a generative model, below 0.60 goes to a human.

The middle band is the interesting case, because the adjudication can fail in
several ordinary ways:

* no LLM provider is configured (the default);
* the provider is configured but the API key is missing;
* the call times out or the provider returns a 5xx;
* the model replies with prose instead of JSON;
* the model returns a label that is not one of the options offered.

Every one of these is *absence of evidence* about whether the decision is correct.
None of them is evidence that it is.

## Decision

When verification cannot be completed, the item escalates to `review`. It never
auto-executes.

Concretely, in `services/workflows.py`:

```python
if verification.status == "ok" and verification.choice:
    run.final_choice = verification.choice
    effective_route = "auto" if verification.agrees else "review"
else:
    # Could not verify -> escalate rather than auto-execute.
    effective_route = "review"
```

The `verification` verdict is still recorded — `status` of `skipped` or `failed`
with an explanatory `error` — so the failure is visible in the ActionLog rather
than implied by the route.

## Consequences

**Good.**

* The failure mode of the whole automation layer is "a human looks at it", not
  "a possibly-wrong action fired unsupervised".
* A misconfigured provider cannot silently degrade a safety gate into a no-op. It
  degrades into a queue.
* The behaviour is testable and is tested, at each failure mode, in
  `tests/test_api_workflows.py`.

**Bad.**

* **A fresh install with no provider configured sends the entire middle band to
  humans.** This is the most common surprise. It is the intended behaviour, and it
  is documented in the getting-started troubleshooting table, but it does look like
  a bug the first time.
* Throughput through the middle band is limited by human capacity until a provider
  is configured. For a high-volume deployment this is a real operational
  constraint, not a theoretical one.
* The operator must reason about two routes for one item (`route` and
  `effective_route`), which is more state to understand than a single value.

## Alternatives considered

**Fail open: execute the automatic action when verification is unavailable.**
Rejected. That makes the confidence gate decorative — an outage in the LLM provider
would silently convert a guarded pipeline into an unguarded one, at the exact
moment nobody is watching.

**Block: return a 5xx and let the caller retry.** Rejected. Loses the decision
entirely, and pushes the problem onto every caller rather than handling it once.

**Retry the verification with backoff, then escalate.** Better, and worth doing —
see [ROADMAP.md](../ROADMAP.md#6-webhook-delivery-hardening). But it changes *how
long* the system tries, not what it does when it gives up. Escalating remains the
correct terminal state, so this ADR holds either way.

**Treat "no provider configured" as a startup error.** Tempting — it is a
configuration mistake, not a runtime failure. Rejected because the provider is
genuinely optional: Laya alone plus a review queue is a coherent product, and
refusing to boot would break that.
