# Mithril Code

Enable the `mithril-code` plugin and connect the selected profile's `MITHRIL_API_KEY`. Code now uses the owned https://code.mithril.fund verification service, whose only inference endpoint is https://api.mithril.fund/v1/chat/completions with qwen/qwen3.8-27b. GitHub credentials are separate.

The model proposes a bounded typed AST. The verifier checks types, allowed operations, both toggle cases and all 511 vectors before deterministic CLJK emission. This is not Jev Decisions, native CLJK execution, arbitrary repository execution or generated UI. Actual model, usage and verification receipt are returned; API cost remains unmeasured. A run consumes Mithril inference and registered Code allowance. No automatic retry or alternate provider exists.

`hermes mithril-code status` reads readiness. `hermes mithril-code run --stdin` reads bounded JSON `{ "goal": "Build a To-do app" }`. Source publication remains a separate user action.

Existing `runner_url` and CODE_RUNNER_TOKEN data are retained but are not used by the new Mithril path. The legacy Python `call_runner` API remains available for compatibility; no fallback invokes it. `code_service_url` defaults to the owned Code origin. Only HTTP loopback may override it for local verification.
