# 0007 — The frontend has no build step

**Status:** Accepted

## Context

The brief asked for an "Open WebUI aesthetic with Tailwind CSS" and a "lightweight
frontend". Open WebUI is a Svelte SPA with a real build pipeline.

The interactive surface here is not large: a form with a textarea, a results panel
rendering a list of probability bars, a few tables, and some buttons. There is no
virtualised list, no optimistic collaborative editing, no client-side routing
beyond ordinary links.

## Decision

Server-render the pages with **Jinja2**, style them with **Tailwind via the CDN**,
and hydrate the interactive parts with **framework-free JavaScript** in
`app/static/js/`.

The pattern throughout is:

1. The page ships an HTML shell with the data it needs for its first paint
   (rendered from the route handler).
2. A per-page script attaches behaviour and calls the JSON API for updates.

So `/analytics` renders its charts from `services/analytics.py` server-side, and
its one interactive control (Refit) is a fetch in `analytics.js`. Conversely
`/` ships a shell and `playground.js` does the work, because almost everything on
it is interactive.

The interactive surfaces that repeat — the dropdown, the tag input, the
key/value and ordered-list editors, the state box, the question cards and the
workflow block script — live in two shared layers rather than being written a
third time:

* `ui.js` upgrades native controls in place. A `<select>` (or an `<input
  list="…">` backed by a `<datalist>`) is restyled into a popup combobox while
  the native element stays in the DOM as the source of truth, so page code keeps
  reading `.value` and listening for `change`. Nothing has to know it happened.
* `question-editor.js` and `block-builder.js` are the two structured editors
  built on top, replacing what used to be JSON textareas in `/schemas`, `/` and
  `/workflows`.

Because those editors nest — a row editor lives inside a question card, which
lives inside a delegated click handler — their buttons use their own
`data-rows-action` namespace rather than the `data-action` the card and block
handlers delegate on. Sharing one attribute made a row's ✕ ambiguous: both
handlers matched it, and which one won depended on whether the inner handler had
already re-rendered the row out of the tree.

## Consequences

**Good.**

* **`pip install -r requirements.txt` and you are running.** No Node, no
  `node_modules`, no lockfile, no build cache, no separate dev server, no CORS
  configuration between two origins.
* The container is ~180 MB and the Dockerfile has no frontend layer.
* Server-rendered pages mean the first paint has real numbers, not a skeleton and
  a spinner. The analytics dashboard is useful before any JavaScript runs.
* Debugging is reading HTML. There is no framework to hold a mental model of.

**Bad.**

* **The Tailwind CDN is an external dependency at page load.** It is a
  render-blocking third-party request, and it breaks an air-gapped deployment
  until someone vendors it by hand
  ([deployment.md](../deployment.md#air-gapped-deployments)).
* **No component model.** `renderBreakdown` and `questionCard` in `static/js/app.js`
  are string builders. They work, and they are shared, but they are not components,
  and there is no type checking between a JSON response and the DOM it produces.
* **No type safety across the boundary.** A renamed field in `schemas.py` breaks a
  template or a script at runtime, not at build time. Mitigated by the API tests,
  which pin the response shape. The editors are exercised the same way — against
  the real templates and scripts, with the API stubbed.
* **In-place upgrading is invisible.** Re-pointing an element's own `value`
  accessor at the native one is a small piece of magic. It is contained in
  `ui.js`, and the alternative — rewriting every call site onto a custom widget
  API — was three times the diff for no behavioural gain.
* The JavaScript is per-page IIFEs. Fine at this size; it would not be at three
  times this size.
* Tailwind's JIT does not run, so the CDN build ships its full ~3 MB runtime and
  compiles in the browser.

## When to revisit

Concretely: if any page needs client-side routing, a virtualised table, real-time
collaboration, or more than roughly a thousand lines of JS. None is on the roadmap.

The **first** fix, when it comes, is not a framework — it is
[compiling a real Tailwind stylesheet at image-build time](../ROADMAP.md#9-tailwind-is-loaded-from-a-cdn).
That removes the CDN dependency without touching the application architecture.

## Alternatives considered

**Vite + React/Svelte SPA, as Open WebUI does.** Closer to the stated aesthetic
reference. Rejected because it requires Node in the toolchain, adds a build stage
and a second origin, and buys nothing the actual UI needs. "Lightweight" was in the
brief, and this is what lightweight means.

**Vite + React with server-side rendering.** Keeps the first-paint property and
adds a component model. Rejected as strictly more machinery than the alternative
above, for the same benefit.

**htmx.** Would simplify several of the interactive parts. Rejected as another
runtime dependency for cases the plain `fetch` calls already handle — and its
model is a poor fit for the playground, which is genuinely stateful client-side.

**Server-rendered only, no JavaScript.** Would be the most honest reading of
"lightweight", and the pages would still work. Rejected because the playground's
live linting and route preview genuinely benefit from client-side interaction, and
a round trip per keystroke is worse than a CDN script.
