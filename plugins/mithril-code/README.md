# Mithril Code in Hermes

Hermes Agent calls the same bounded Jev/Mithril runner used by [Code](https://code.mithril.fund/). This owned plugin registers the `mithril_code` toolset and `hermes mithril-code` CLI, without changing the conversation loop or prompt during a session. Enable it through the existing plugin controls and start a new conversation with this toolset enabled.

Configure the owning profile's `config.yaml`:

```yaml
plugins:
  enabled: [mithril-code] # preserve any other enabled plugins
  entries:
    mithril-code:
      settings:
        runner_url: http://127.0.0.1:5184
```

Store the existing `CODE_RUNNER_TOKEN` in that profile's secret store/.env, never YAML. Use an explicitly trusted HTTPS tunnel when the runner is on another machine; HTTP is restricted to loopback. The runner remains the existing Fund `apps/code/runner/server.mjs`; configure its trusted Mithril/NBB/classpath/policy and model credential according to its README. No runtime or model key is automatically installed, shared between profiles or inferred from another repository. A runner token grants model execution against that runner, so only authorized profiles should receive it.

```sh
hermes mithril-code status
printf '%s' '{"goal":"Build completion toggle and remaining count"}' | hermes mithril-code run --stdin
```

`status` is a live readiness probe. `run` sends only the brief and fixed `todo` template to the authenticated runner, incurs its configured model cost, and returns verified CLJK source, typed AST and real receipts. Desktop uses this exact CLI. It neither executes uploaded repository code nor writes, commits or publishes source. Review source before saving through existing file/GitHub tools. Timeouts are uncertain and must not be automatically retried. Runner-provided verification is not remote attestation. Redirects and ambient proxies cannot forward the credential.

The System One example is the finite, stage-specific typed choice sequence, followed by immutable host checks and exact AST/source replay. See the [recorded To-do case](case-study.md). This is a bounded pilot, not a claim of general coding accuracy.
