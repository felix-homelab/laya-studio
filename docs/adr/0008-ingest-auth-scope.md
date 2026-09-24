# 0008 — Ingestion is the only authenticated surface

**Status:** Accepted

## Context

Laya Studio has one endpoint family designed to be called by *third parties*:
`/api/ingest/*`. A webhook fires it. A mail worker fires it. A caller you do not
control fires it, from a network you do not control.

Everything else is an operator surface: the playground, the schema editor, the
analytics dashboard, the review queue. It is used by a person sitting in front of
it.

The question is where to put an authentication boundary — and how much of one.

## Decision

Authenticate `/api/ingest/*` with a single shared token, and leave every other
endpoint open.

* `LAYA_STUDIO_INGEST_TOKEN` set → `X-Ingest-Token` is required, compared with
  `hmac.compare_digest` (constant time).
* Unset → ingestion is open.
* The guard is applied at the router level (`dependencies=[Depends(require_ingest_token)]`),
  so it covers every ingestion route including ones added later.
* The open/unauthenticated state is surfaced in the UI — the workflows page states
  whether a token is required.

The rest of the panel is documented as needing to sit behind the operator's own
access control. See [deployment.md](../deployment.md#hardening).

## Why not authenticate everything

Because half-authentication is worse than none. A password on the panel implies the
panel is safe to expose; it would need session management, password storage, CSRF
protection, and a story for password reset — none of which a single-operator tool
should be inventing. Put it behind an identity-aware proxy, which is where that
problem is already solved properly.

Ingestion is different in kind: it cannot be behind a login, because the caller is
a machine with no session. So it needs its own mechanism, and a shared token is the
right size for it.

## Consequences

**Good.**

* The one genuinely dangerous surface — an endpoint that can trigger outbound
  webhooks — is guarded by default when configured, and the guard is at the router
  level so it cannot be forgotten on a new route.
* Constant-time comparison means the token cannot be recovered byte by byte.
* `test_the_token_guard_does_not_leak_to_other_routers` pins the scope, so a future
  change cannot accidentally widen it.
* Zero friction for local use: unset the token, everything works.

**Bad.**

* **A single shared token is not per-caller.** There is no per-caller attribution,
  no revocation of one caller, and no rotation without downtime. For anything
  multi-tenant this is insufficient.
* **The default is open.** `LAYA_STUDIO_INGEST_TOKEN` is unset, so a panel deployed
  without reading the docs has an open ingestion endpoint. This is a real
  deployment footgun. Mitigated by the UI stating the state and by the hardening
  checklist, not fixed by the code.
* **Everything else really is open.** Anyone who can reach the port can edit
  schemas and read every evaluation. That is the design, and it must be paired with
  network-level access control.

## Alternatives considered

**OAuth2 / OIDC with user accounts.** Proper, and wrong for the scope. It would
triple the project's size and duplicate a solved problem. Rejected — see the
[roadmap](../ROADMAP.md#multi-tenancy-and-user-accounts).

**Per-caller API keys in a database.** Would fix attribution and revocation, and is
the right next step if ingestion becomes multi-tenant. Rejected *for now* because it
requires a key-management UI, hashing strategy and rotation flow — more surface
than a single-operator panel justifies.

**HMAC request signing instead of a bearer token.** Protects against replay and
tampering, not against unauthorised access, which is the actual threat here.
Rejected as solving the wrong problem (though it *is* the right fix for the
outgoing webhooks — tracked in the [roadmap](../ROADMAP.md#6-webhook-delivery-hardening)).

**No authentication at all, documented as "internal only".** Simplest and honest
about the threat model. Rejected because ingestion is specifically designed to be
called by machines on other networks, and "internal only" is an assumption that
quietly stops being true.
