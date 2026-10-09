---
sidebar_position: 9
title: "Tools Runtime"
description: "Runtime behavior of the tool registry, toolsets, dispatch, and terminal environments"
---

# Tools Runtime

Hermes tools are self-registering functions grouped into toolsets and executed through a central registry/dispatch system.

Primary files:

- `tools/registry.py`
- `model_tools.py`
- `toolsets.py`
- `tools/terminal_tool.py`
- `tools/environments/*`

## Tool registration model

Each tool module calls `registry.register(...)` at import time.

`model_tools.py` is responsible for importing/discovering tool modules and building the schema list used by the model.

### How `registry.register()` works

Every tool file in `tools/` calls `registry.register()` at module level to declare itself. The function signature is:

```python
registry.register(
    name="terminal",               # Unique tool name (used in API schemas)
    toolset="terminal",            # Toolset this tool belongs to
    schema={...},                  # Model-facing schema (description, parameters)
    handler=handle_terminal,       # The function that executes when the tool is called
    check_fn=check_terminal,       # Optional: returns True/False for availability
    requires_env=["SOME_VAR"],     # Optional: env vars needed (for UI display)
    is_async=False,                # Whether the handler is an async coroutine
    description="Run commands",    # Optional ToolEntry registry metadata
    emoji="💻",                    # Emoji for spinner/progress display
)
```

Each call creates a `ToolEntry` stored in the singleton `ToolRegistry._tools` dict keyed by tool name. A registration that would shadow an existing tool from a **different** toolset is rejected (with an error log) unless the caller passes `override=True`; plugin overrides of built-in tools additionally require the operator opt-in `plugins.entries.<plugin_id>.allow_tool_override: true` in `config.yaml`.

`schema["description"]` is the authoritative model-facing description. The separate `description=` argument populates `ToolEntry.description`; when it is omitted, the registry metadata falls back to the schema description. `get_definitions()` builds the OpenAI function definition from `entry.schema` and does not copy `entry.description` into a schema that lacks `description`. Therefore, `description=` alone does not describe the tool to the model, and when both values differ the model sees the schema value. Prefer defining the description once in the schema unless a registry consumer intentionally needs different metadata.

### Discovery: `discover_builtin_tools()`

When `model_tools.py` is imported, it calls `discover_builtin_tools()` from `tools/registry.py`. This function scans every `tools/*.py` file using AST parsing to find modules that contain top-level `registry.register()` calls, then imports them:

```python
# tools/registry.py (simplified)
def discover_builtin_tools(tools_dir=None):
    tools_path = Path(tools_dir) if tools_dir else Path(__file__).parent
    for path in sorted(tools_path.glob("*.py")):
        if path.name in {"__init__.py", "registry.py", "mcp_tool.py"}:
            continue
        if _module_registers_tools(path):  # AST check for top-level registry.register()
            importlib.import_module(f"tools.{path.stem}")
```

This auto-discovery means new tool files are picked up automatically — no manual list to maintain. The AST check only matches top-level `registry.register()` calls (not calls inside functions), so helper modules in `tools/` are not imported.

Each import triggers the module's `registry.register()` calls. Errors in optional tools (e.g., missing `fal_client` for image generation) are caught and logged — they don't prevent other tools from loading.

After core tool discovery, MCP tools and plugin tools are also discovered:

1. **MCP tools** — `tools.mcp_tool_discovery.discover_mcp_tools()` (re-exported by the `tools.mcp_tool` facade) reads MCP server config and registers tools from external servers.
2. **Plugin tools** — `hermes_cli.plugins.discover_plugins()` loads user/project/pip plugins that may register additional tools.

## Tool availability checking (`check_fn`)

Each tool can optionally provide a `check_fn` — a callable that returns `True` when the tool is available and `False` otherwise. Typical checks include:

- **API key present** — e.g., `lambda: bool(os.environ.get("SERP_API_KEY"))` for web search
- **Service running** — e.g., checking if the Honcho server is configured
- **Binary installed** — e.g., verifying `playwright` is available for browser tools

When `registry.get_definitions()` builds the schema list for the model, it runs each tool's `check_fn()`:

```python
# Simplified from registry.py
if entry.check_fn:
    try:
        available = bool(entry.check_fn())
    except Exception:
        available = False   # Exceptions = unavailable
    if not available:
        continue            # Skip this tool entirely
```

Key behaviors:
- Check results are **cached per-call** — if multiple tools share the same `check_fn`, it only runs once.
- Exceptions in `check_fn()` are treated as "unavailable" (fail-safe).
- The `is_toolset_available()` method checks whether a toolset's `check_fn` passes, used for UI display and toolset resolution.

## Toolset resolution

Toolsets are named bundles of tools. Hermes resolves them through:

- explicit enabled/disabled toolset lists
- platform presets (`hermes-cli`, `hermes-telegram`, etc.)
- dynamic MCP toolsets
- curated special-purpose sets like `hermes-acp`

### How `get_tool_definitions()` filters tools

The main entry point is `model_tools.get_tool_definitions(enabled_toolsets, disabled_toolsets, quiet_mode)`:

1. **If `enabled_toolsets` is provided** — only tools from those toolsets are included. Each toolset name is resolved via `resolve_toolset()` which expands composite toolsets into individual tool names.

2. **If `disabled_toolsets` is provided** — start with ALL toolsets, then subtract the disabled ones.

3. **If neither** — include all known toolsets.

4. **Registry filtering** — the resolved tool name set is passed to `registry.get_definitions()`, which applies `check_fn` filtering and returns OpenAI-format schemas.

5. **Dynamic schema patching** — after filtering, `execute_code` and `browser_navigate` schemas are dynamically adjusted to only reference tools that actually passed filtering (prevents model hallucination of unavailable tools).

### Legacy toolset names

Old toolset names with `_tools` suffixes (e.g., `web_tools`, `terminal_tools`) are mapped to their modern tool names via `_LEGACY_TOOLSET_MAP` for backward compatibility.

### Remote Python child-call delivery

`tools/code_execution_rpc.py::_rpc_poll_loop` serves both the persistent remote
Python kernel and the per-call remote script transport. After authentication and
sequence validation, it atomically creates an owner-only `dispatch_<sequence>`
directory and moves the request into it **before** invoking the existing tool
dispatcher. A failed claim does not dispatch. Markers remain for the RPC directory's
lifetime; reusing a claimed sequence cannot grant another execution.

Result shipping checks the actual remote command exit status. If shipping fails,
the original request is no longer in the polling queue and cannot be automatically
executed again. The missing response remains an unknown outcome for the caller;
the marker is not a completion receipt or proof of cancellation. This protects
read, write and provider calls from transport-induced replay without changing the
existing tool allowlist, approval callbacks, profile scope or per-cell call budget.
RPC-directory cleanup belongs to the existing kernel/script lifecycle.

`tests/tools/test_remote_kernel_file_rpc_live.py` also starts the actual detached
Python runner through a credential-free local shell transport. Two cells retain
the same process and Python namespace, use the real generated `hermes_tools`
module and registry reader, enforce the first cell's child budget, and let the
second cell use fresh sequence claims and its own budget. Owner shutdown is
verified by process liveness and removal of the owned temporary directory. This
qualifies the remote **file protocol** locally; it does not qualify SSH, a hosted
execution environment, inference or a distributed cancellation receipt.

Kernel identity also includes the canonical `hermes_home_key()`. A conversation
id, task id or delegated-child id is not globally unique across profiles. Both
local and remote kernels capture the resolved profile at creation, retain reuse
within that profile, and refuse to share Python state with another profile that
uses the same owner id. Owner and delegated-child shutdown select only the calling
profile's kernels; explicit unselected process-wide shutdown still closes all.
Off-turn cleanup callers must bind the owning profile's full runtime scope, as
session finalization and delegation already do.

The same real-process test file covers A→B→A with identical owner, interpreter,
working directory and tool selection in both local and remote transports. B cannot
read A's namespace, returning to A retains its value, shutting down A preserves B,
and reopening A starts clean. The preceding source fails both variants because B
sees A's variable. This verifies profile state and teardown isolation, not dynamic
schema admission, provider readiness, remote-host authentication or all tool effects.

The real-shell regression in `tests/tools/test_code_execution_file_rpc.py` invokes
the actual registry file reader and fails result transport, then observes later
polls. The unchanged base executes the same request twice; the fixed path executes
once, retains its private claim and produces no false response. The generated-stub
test also covers call correlation, authentication, allowed operations and budgets.
This is local transport evidence, not a live remote-provider or installed-client
qualification. Full Web/Desktop tool-only dispatch, durable cross-runtime attempt
receipts, live revision admission and cancellation verification remain separate.

## Dispatch

At runtime, tools are dispatched through the central registry, with agent-loop exceptions for some agent-level tools such as memory/todo/session-search handling.

### Dispatch flow: model tool_call → handler execution

When the model returns a `tool_call`, the flow is:

```
Model response with tool_call
    ↓
agent loop (`agent/conversation_loop.py`, via `run_agent.py`'s `AIAgent` facade)
    ↓
model_tools.handle_function_call(name, args, task_id, user_task)
    ↓
[Agent-loop tools?] → handled directly by agent loop (todo, memory, session_search, delegate_task)
    ↓
[Plugin pre-hook] → invoke_hook("pre_tool_call", ...)
    ↓
registry.dispatch(name, args, **kwargs)
    ↓
Look up ToolEntry by name
    ↓
[Async handler?] → bridge via _run_async()
[Sync handler?]  → call directly
    ↓
Return result string (or JSON error)
    ↓
[Plugin post-hook] → invoke_hook("post_tool_call", ...)
```

### Error wrapping

All tool execution is wrapped in error handling at two levels:

1. **`registry.dispatch()`** — catches any exception from the handler and returns `{"error": "Tool execution failed: ExceptionType: message"}` as JSON.

2. **`handle_function_call()`** — wraps the entire dispatch in a secondary try/except that returns `{"error": "Error executing tool_name: message"}`.

This ensures the model always receives a well-formed JSON string, never an unhandled exception.

### Agent-loop tools

Four tools are intercepted before registry dispatch because they need agent-level state (TodoStore, MemoryStore, etc.):

- `todo_list` — planning/task tracking
- `memory` — persistent memory writes
- `session_search` — cross-session recall
- `delegate_task` — spawns subagent sessions

These tools' schemas are still registered in the registry (for `get_tool_definitions`), but their handlers return a stub error if dispatch somehow reaches them directly.

### Async bridging

When a tool handler is async, `_run_async()` bridges it to the sync dispatch path:

- **CLI path (no running loop)** — uses a persistent event loop to keep cached async clients alive
- **Gateway path (running loop)** — spins up a disposable thread with `asyncio.run()`
- **Worker threads (parallel tools)** — uses per-thread persistent loops stored in thread-local storage

## The DANGEROUS_PATTERNS approval flow

The terminal tool integrates a dangerous-command approval system defined in `tools/approval.py`:

1. **Pattern detection** — `DANGEROUS_PATTERNS` is a list of `(regex, description)` tuples covering destructive operations:
   - Recursive deletes (`rm -rf`)
   - Filesystem formatting (`mkfs`, `dd`)
   - SQL destructive operations (`DROP TABLE`, `DELETE FROM` without `WHERE`)
   - System config overwrites (`> /etc/`)
   - Service manipulation (`systemctl stop`)
   - Remote code execution (`curl | sh`)
   - Fork bombs, process kills, etc.

2. **Detection** — before executing any terminal command, `detect_dangerous_command(command)` checks against all patterns.

3. **Approval prompt** — if a match is found:
   - **CLI mode** — an interactive prompt asks the user to approve, deny, or allow permanently
   - **Gateway mode** — an async approval callback sends the request to the messaging platform
   - **Smart approval** — optionally, an auxiliary LLM can auto-approve low-risk commands that match patterns (e.g., `rm -rf node_modules/` is safe but matches "recursive delete")

4. **Session state** — approvals are tracked per-session. Once you approve "recursive delete" for a session, subsequent `rm -rf` commands don't re-prompt.

5. **Permanent allowlist** — the "allow permanently" option writes the pattern to `config.yaml`'s `command_allowlist`, persisting across sessions.

## Terminal/runtime environments

The terminal system supports multiple backends:

- local
- docker
- ssh
- singularity
- modal
- daytona
- vercel_sandbox

It also supports:

- per-task cwd overrides
- background process management
- PTY mode
- approval callbacks for dangerous commands

`tools/process_registry_checkpoint.py` owns running-process checkpoints and
PID-safe adoption. Completed output is separate: `tools/process_registry_results.py`
writes one atomic, redacted receipt per process under the profile's
`logs/process-results/`. Producers cannot overwrite another parent's results by
rewriting the shared PID checkpoint. The registry persists the receipt before
releasing its completion event; one-shot linger waits on that event. The existing
process query methods load retained snapshots without adopting PIDs or enqueuing
notifications. Reads require the commissioning durable session or its compression
continuation; knowing a handle alone does not authorize a retained result read.
The registry captures that owner before starting any output reader, including on
CLI and non-notifying processes, and preserves the producer's profile context in
reader threads. Receipt redaction is forced independently of live-output opt-out;
retention is bounded by age and count.

## Concurrency

Tool calls may execute sequentially or concurrently depending on the tool mix and interaction requirements.

The generated local socket client (Unix socket or loopback TCP) serializes each
child request/response. A persistent kernel may reconnect if connection setup
fails or an old socket is already at EOF before any operation is sent. Once
`sendall` starts, a partial send, disconnect or lost response is an unknown
outcome: close the socket, report that the request was not retried, and never
resend it automatically. Even a complete host-side write does not establish
that its receipt reached the child. A later independent call can use a fresh
connection. Real socket/process tests cover a dispatched registry file write
with lost response and a stale socket before send, for both transports on the
test host; this does not establish installed Windows or distributed receipt
recovery.

Single-call agent dispatch (`AIAgent._invoke_tool`, also used by concurrent workers)
resolves currently exposed context-engine names through the owning agent's
`context_compressor.handle_tool_call`, passing the live `messages` list. These
plugin tools need not be registered in the global tool registry. Existing inline
executors retain precedence; context-engine names precede memory-provider and
registry dispatch. Removing a name from the agent's context-engine set retires
that route immediately. Callers still bind the owning profile and acquire the
normal turn authority; this resolver is not a standalone tool-only RPC, lease,
budget or durable receipt contract.

Inline route selection captures the owning context engine's bound handler and
the canonical memory manager's selected provider and bound handler. Replacing
an engine, provider mapping or provider handler after selection cannot redirect
that invocation. Memory-provider metrics and error normalization still run
through `MemoryManager`; route selection does not bypass that observer.

Parent-bound Python children additionally capture these execution identities
alongside their schemas. Recheck before policy and immediately before invocation;
the host-only `agent/inline_dispatch_binding.py` binding checks again at the
canonical inline resolver. A policy-time replacement settles as `rejected`
without a dispatch timestamp. A replacement after the agent's check but before
inline resolution returns an error through the started invocation and settles
as `returned-error`. Neither case calls the replacement. Changes after selection
cannot redirect the captured callable. Children receive the agent's live
`_session_messages` list without appending child transcript rows.

Inline table selection also retains the actual todo/memory store, memory-write
notification and metadata builder, recall getter, GUI/connection callbacks and
delegation callback. `inline_target` reads those retained references during the
selected invocation; an agent attribute replacement cannot redirect it after
the last resolver check. Parent snapshots include those identities, so a
replacement during policy or before invocation is rejected through the same
attempt states described above. Data inside the selected store remains live:
ordinary todo writes still advance its revision and do not retire its owner.

This pins execution references, not plugin/store internals, the recall getter's
eventual database, GUI target/grant revisions, custom manager internals or a
deployed executor revision. Those require their own operation admission and
qualification evidence. The production code sandbox allowlist is unchanged.

During normal agent `execute_code` dispatch, `agent/code_child_dispatch.py` binds
the existing local socket and remote file-RPC child consumers to the owning
agent's middleware, plugin/pruned-argument checks, guardrails, approval callbacks,
checkpoint preflight and mutation observation. Parent task, profile, session,
turn and worker interruption are checked before admission and again before the
effect dispatch. Names are limited to the parent's starting scope and intersected
with its current scope; the existing code sandbox allowlist and per-cell budget
still apply. The binding retires when the parent dispatch exits, including for
callbacks captured by old cell contexts. Standalone code-tool callers retain the
existing registry route.

Child results remain structured data: guardrail counters observe them without
replacing repeated results with model-transcript stubs or appending guidance.
No child message is inserted into conversation history. This is an in-turn
policy connection, not an external tool-only RPC, expanded code allowlist,
distributed budget/attempt ledger, confirmed cancellation or installed-runtime
qualification.

When that parent has an existing session database, code child dispatch records
private attempt metadata in its profile-owned `session_tool_attempts` table.
Claim an opaque host attempt ID before policy, commit `running` immediately before
the handler, and settle with compare-and-set after it returns. Claims and terminal
states cannot be overwritten or reused to dispatch again. A thrown/lost result
after the effect leaves a nonterminal `running` record across database reopen;
it must not be interpreted as no effect or permission to retry. A `returned`
record proves handler return, not successful result delivery or confirmed stop;
`returned-error` may still have effects.

Rows retain parent call ID, tool name, timestamps, request/result digests and
result byte count, not arguments, credentials or result bodies. Reads use both
session ID and attempt ID inside the same profile database. Session deletion
cascades the metadata. A replaced/foreign-profile database is rejected. Callers
without a session database keep their existing in-process path. This is connected
to the real local/remote code child consumers. The owned `tools.attempts` RPC
reads bounded metadata from the attached session without dispatch or retry;
it does not restore result payloads, supply stable client replay IDs or provide
a distributed attempt/parent-budget contract.

## Owned tool-only RPC

`tools.call` explicitly invokes one tool on an already built, attached agent and
existing profile-owned durable session. It does not create/resume a conversation,
submit a prompt, or start a second orchestration/model loop. A tool such as
delegation or search may perform its own normal provider work. The caller supplies
the current `tools.show.runtime_snapshot` context ID and server-local revision;
that observation is checked against live state and never grants execution alone.

The gateway claims the idle session under its existing admission lock, takes the
cross-process conversation lease without waiting for another owner, binds the
full profile/session/approval context, and uses the existing parent-bound agent
dispatch. Normal request/execution middleware, guardrails, hooks, approval,
mutation observation, frozen schemas and handler/store/callback fences apply.
Root `execute_code` is admitted through the same policy and its children inherit
the RPC's live attachment/context/lease/deadline authority. The production code
sandbox allowlist remains unchanged.

For owned RPC dispatch, the exact finite JSON arguments are captured before
middleware or plugin hooks run. Immediately before the handler, the dispatcher
compares a private JSON copy against that admitted intent. A changed target,
content or code is rejected without a dispatch timestamp; retries read the
terminal attempt metadata rather than executing the rewrite. Nested code children
inherit this fence. Equivalent object key ordering remains valid. Ordinary
model-turn middleware rewrites keep their existing behavior. This argument fence
does not replace a complete operation effect/target manifest or provider-specific
authorization.

The execution task is stable for the owning durable session/profile, so successive
root Python cells keep their variables without sharing them across profiles.
Each request still receives a distinct turn/parent-call identity; no stale model
API request ID is attached to tool-only work. In-process delegation inherits the
owning agent context, rather than commissioning another orchestration loop.

The caller's bounded `request_id` maps to `rpc:<request_id>` inside the exact
durable session/profile. The same request returns attempt metadata only; a
different request reusing that ID is rejected. Neither case redispatches. The
gateway does not store result bodies. A write whose result is lost remains
nonterminal `running`, and retry only reads that metadata. Results distinguish
`handler-return`, `policy-result`, `metadata-only` and `unknown`; handler return
does not prove client delivery or confirmed cancellation.

`timeout_ms` bounds admission and nested child admission; after its deadline,
lost authority or an oversized/unserializable result, withhold output and report
unknown observation. This does not prove an already started handler/process has
stopped. The lease is maintained while the synchronous handler is outstanding and
released when it unwinds. Long RPC handlers run in the existing worker pool so
the reader can still process approval answers and `session.interrupt`.

This is gateway source/local functionality, not Web/Desktop host-adapter wiring,
deployed-runtime qualification, a shared SDK schema hash/effect manifest, native
target grants, distributed parent budgets/ledger or result/artifact recovery.
Dynamic bridge/plugin internals and each provider/write/scheduler/delegation/MOA
operation still need their own effect admission and live qualification evidence.

### Effect manifest readback and invalidation

`registry.register(effect_manifest=...)` and the optional keyword-only
`PluginContext.register_tool(effect_manifest=...)` accept bounded, finite, host-authored
partial declarations alongside the schema and handler. The descriptor contains
`coverage: "partial"`, effect names and target descriptors (`kind`, an argument
JSON pointer, and a resolution label). Registration copies the value, rejects
malformed declarations and cannot claim complete coverage. `write_file` declares
its known file-write/path effect against the selected terminal runtime; backend
setup, backups and other possible effects are not exhaustively qualified by that
partial declaration.

`tools.show.runtime_snapshot.effect_manifests` covers exactly the frozen
model-visible names in the attached profile. Tools without explicit metadata,
including annotation-only MCP tools and inline tools, report `coverage: "unknown"`.
Declarations are not inferred from names or schema annotations. Discovery does
not grant permission, resolve a target, or prove provider readiness.

The server-local revision covers both frozen schemas and these descriptors.
Observed declaration changes retire the opaque context, including A→B→A;
malformed metadata retires it before refusing readback. The registered-child
handler binding also fingerprints the declaration, catching a change between
policy and registry handler selection. Model prompt schemas/history stay frozen.
Existing Web/Desktop owned adapters compare this same opaque context/revision.

This is the shared metadata/invalidation foundation, not a full-operation manifest
or resolved target grant. Per-operation declarations, actual target resolution,
host approval display and grants, shared budgets, async completion and real
provider/native qualification remain required.

The snapshot also captures registrations under the same profile/registry lock as
the descriptors. Private entry, handler, async flag and schema/manifest captures
retire context when observed to change, even if the public descriptors/revision
are identical. Restoring a previous callable never revives an observed old
context. Function pointers, registration objects and profile paths are not added
to the wire. The existing registered-handler fence still pins the callable during
an admitted call. This covers registered execution targets; inline store/callback
and provider configuration generations require their own admission audit.

### Owned local write target recheck

The builtin `write_file` registration supplies a private `dispatch_target`
resolver using the same selected-task local path resolver as the file handler.
Owned dispatch captures its finite JSON target before middleware and compares
it immediately before marking the handler dispatched. A symlink retargeted by
request/execution middleware or a pre-tool hook is refused, even when arguments
are unchanged. Root calls and Python `execute_code` children use the same fence;
ordinary model-origin dispatch retains its existing behavior. Resolver identity
is part of the private registration/context capture and never appears on the wire.
Expected target-resolution failures at admission remain durable policy refusals,
with no dispatched timestamp; duplicate requests return metadata only.

This is a path recheck, not a resolved target in the human approval preview or a
target grant. SSH/container namespaces return no binding rather than interpreting
their paths on the host. Other operations remain unbound. It does not pin an inode
or prevent a filesystem race after the final recheck. Full operation declarations,
preview-to-executor target identity, namespace-specific resolution and atomic file
effects remain separate qualification gates.

### Owned target preview contract

`tools.target_preview` reads the explicitly attached, idle, already built agent's
frozen tool context. It uses the same selected-task overrides as `tools.call` and
does not invoke a handler, create a runtime or grant permission. A registered
resolver returns a bounded partial target plus a content digest covering owner,
task, context, revision, name, exact finite JSON arguments and resolved target.
Unsupported resolvers/namespaces return `target_binding: null`; tools outside the
frozen conversation and foreign transport owners are refused.

`tools.call(target_digest=...)` checks that identity before admission and throughout
its existing authority checks, including after middleware. The durable request
digest also includes the target digest, so a request ID cannot be reused for a
different preview identity. Identical duplicates remain metadata-only. Omitting
the additive field preserves existing callers and their request digest.

The digest is target identity, not a signed grant or complete effect declaration.
Only local `write_file` currently supplies a resolver. This endpoint is locally
qualified through the real owned RPC/agent/file handler; Mithril workspace agency.15 candidate consumes it through the shared SDK,
API relay and human approval/grant identity in local loopback qualification.
That evidence retains provider/issuer/profile fixtures and does not publish the
package or establish an installed Desktop dependency. Namespace-specific targets, inode/atomic file effects, distributed
budgets and public/installed qualification remain required. The shared selected-task
override preparation now lives in `tui_gateway/owned_tool_targets.py`.

### Cross-language owned SDK qualification

The opt-in `tests/tui_gateway/test_owned_tool_call.py` cross-repository qualifier
runs the already-built Mithril SDK in a real Node subprocess. Its JSON-RPC pipes
reach the actual Hermes dispatcher, worker pool, StdioTransport, agent tools and
profile-owned SessionDB. Provider construction and the four-tool inventory are
controlled fixtures; no inference or provider request runs. Test-only controls
select the already-built profile and mark temporary files to detect replay.

Run with the canonical runner, forwarding the explicit pytest option after `--`:

```sh
HERMES_PYTHON="$PWD/.venv/bin/python" scripts/run_tests.sh \
  tests/tui_gateway/test_owned_tool_call.py -j 2 -- \
  --owned-sdk-module=/path/to/built/workspace/dist/owned-gateway-tools.js
```

Without that module or Node the cross-repository cases skip, rather than pretending
to qualify integration. Successful cases verify A→B→A read/write/todo/root Python,
profile-separated persistent cells, actual nested child reads, metadata-only
write/Python replay, foreign ownership rejection and real attempt readback.
The lost-result case performs a real temporary write before withholding the
handler result; stable replay preserves `running`/unknown and the marked file.
This is local pipe integration, not an authenticated WebSocket connection,
Web HTTP grant wiring, a real QuickJS/Pyodide parent, installed client, external
provider or publication qualification.

## Related docs

- [Toolsets Reference](../reference/toolsets-reference.md)
- [Built-in Tools Reference](../reference/tools-reference.md)
- [Agent Loop Internals](./agent-loop.md)
- [ACP Internals](./acp-internals.md)

### Authenticated local WebSocket qualification

The same explicit SDK qualifier also starts a private loopback ASGI server with
the real dashboard `/api/ws` router, ticket consumer and WSTransport. A real Node
WebSocket client presents a server-minted single-use ticket in the subprotocol;
only `hermes-gateway-v1`, not the credential, is reflected. No credential and an
already-consumed ticket both fail the upgrade. Actual `session.resume` reattaches
the already-built profile agent, and the compiled SDK reads/writes through the
real dispatcher. A fresh ticket reconnects to that same runtime; stable write
replay returns metadata without overwriting the marked temporary file, and the
owned attempt reader returns the stored state.

The issuer identity, two-profile inventory and profile-home resolver are test
fixtures, and background watchers are disabled for bounded teardown. Ticket
validation, the actual route/upgrade/transport, reattachment, handlers, files and
DB are real. This does not establish OAuth login, a public account, the Fund
Worker relay/HTTP grant wiring, installed Desktop behavior or all-operation
readiness. The listener, process and thread are closed after qualification.

### Peer response ownership and actual approval queue

An `srq-` ID identifies a pending question; knowing it does not authorize an
answer. Network peer responses, `request.answer`, and `clarify.lock` check the
ContextVar-bound transport's attachment to the original live session record,
profile home and durable session key captured when the request was minted.
Known authenticated actor/owner mismatches are refused. Legacy token and stdio
transports retain their existing attachment authority; this does not redesign
session attachment or establish full principal isolation. Replaced records and
changed profile/session keys cannot answer an older request. Approval choices
must be among the choices actually offered. Internal first-settlement identity
checks preserve cancellation and duplicate-answer behavior.

Compute-host mirrors retain the same private captured scope; response/lock
forwarding checks it before changing the mirror. The child binds its existing
host transport while processing forwarded answers. No new wire fields or model
conversation are introduced.

The owned-call qualifier also starts the real `_await_gateway_decision` wait
and `_emit_approval_request` registration. Actual dispatcher responses must leave
the queue pending for foreign transports, unoffered choices, changed profiles
and replaced sessions. A matching response resolves it once; cancellation and
an unsent/lost reply withdraw without inventing consent. With the explicit
compiled-module option, Node imports the sibling `owned-gateway-approvals.js`,
captures the real request, rejects foreign/unoffered/replayed choices and returns
the original-ID response which the actual dispatcher resolves in that queue.
This case uses captured frames and a Node subprocess, not a mounted Web/Desktop
card, production socket or installed-client approval/effect qualification.

The optional mounted qualifier adds the production Web and Desktop client source
paths to the canonical command above:

```sh
--approval-ui-web-client=/path/to/fund/apps/web/src/routes/app/dashboard-client.ts \
--approval-ui-desktop-client=/path/to/desktop/src/renderer/src/screens/Chat/dashboardGatewayClient.ts
```

These flags are qualification inputs, not runtime configuration. Both sources and
the compiled agency.12 SDK must be present; the SDK's owning dependency directory
must supply esbuild/jsdom/React. The qualifier bundles the actual clients with the
compiled shared card, mounts it in jsdom and connects real Node WebSockets to the
actual private loopback dashboard ticket route. Begin/cancel/state HTTP routes
exist only in that temporary test app and control the real queue wait. The
credential-delivery adapter supplies server-minted test tickets. Both surfaces
verify explicit choice, cancellation, disconnect and a fresh-ticket resume which
restores the original peer ID; only a new click resolves it. All threads, sockets
and waits are closed. React browser-bundle scheduler ports require explicit Node
exit after successful assertions and client closure.

This is production-client/canonical-card local network qualification. It does not
mount the whole Web screen/Desktop hook or establish the actual Fund Worker relay
network, public OAuth, original public chat, installed client, provider inference,
tool effects or full principal isolation. The small card harness applies captured
cancel/close notices; the production consumer screen/hook remains a separate gate.
