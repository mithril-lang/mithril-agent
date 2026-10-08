# Owned attempt metadata

`tools.attempts` reads durable tool attempt metadata for an explicitly attached live session. Params require `session_id`; `limit` defaults to 50 and is an integer from 1 to 100. `before_attempt_id` optionally advances from a cursor returned by the previous page. Cursors are scoped to the same durable session and profile. There is no omitted-session settings fallback.

The gateway checks transport membership and the live session generation before and after reading the canonical profile SessionDB under home, secret and terminal scope. Missing DB/session storage reports `available: false`. Foreign, missing or replaced attachments fail with 4001. An unavailable cursor or raced page fails with 5036. No tool dispatch, inference, grant or retry is performed.

The result protocol is `hermes-tool-attempts-v1`, coverage `exact-session-metadata-only`. Pages order by creation time then attempt ID, newest first, and contain at most `limit` rows plus an optional `next_cursor`. Concurrent settlements may change state between pages; this is not a frozen cross-page snapshot. Rows retain host attempt/parent IDs, tool name, state, terminal flag, timestamps and result digest/byte count. They omit request digests, arguments, credentials and result bodies.

`pending` and `running` remain nonterminal. `returned` establishes handler return, not successful delivery, confirmed stop or complete effects. Error returns may have partial effects. Reading a record never authorizes replay. Stable client replay IDs, payload/artifact recovery and a distributed attempt ledger remain separate work.
