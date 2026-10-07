# Original Mithril schedule custody client (draft)

This owned plugin connects the original Agent to the account-bound custody protocol at `api.mithril.fund`. It adds an internal/operator CLI command, not a model tool, new scheduling parser or alternate Schedules screen. Original cron/once/interval definitions and microsecond occurrence identities are passed unchanged.

Enable through the existing plugin configuration. The selected profile's existing `MITHRIL_API_KEY` is resolved through `agent.secret_scope`; do not add behavioral environment variables or copy credentials between profiles. Production requests use the fixed Mithril API origin. HTTP loopback origins exist only for local qualification. Ambient proxies and redirects are disabled.

`hermes mithril-schedule-custody --stdin` reads a bounded JSON envelope with `owner` and `command`. Commands are the exact API `status`, `select`, `claim` or `transition` payloads. Caller-supplied executor or credential identities are not supported. Receipts must match the expected account/profile/operation and exact result schema before they are returned. Replayed starts return `changed: false`; a lost acknowledgement returns an unknown outcome without retry. No script, tool or model is executed by this command.

Qualification covers real plugin discovery across A/B/A homes with different scoped credentials, real HTTP requests, redirected/lost acknowledgements, foreign/extra receipts, replayed starts, precise original timestamps and bounded requests. The API's separate workerd D1/R2 qualification covers actual server custody and authentication.

This plugin is not yet registered as an automatic cron dispatch policy. Private source anchors, original scheduler start/completion/error fencing, unknown-result reconciliation, Native background synchronization, shared original Schedules mounting, current-main publication and installer qualification remain required. Never infer scheduler activation from a successful CLI status/claim. API migration0050 and authenticated route publication remain separate delivery gates. Existing scopes are not silently upgraded.
