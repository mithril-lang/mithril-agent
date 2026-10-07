# Mithril Code harness

The existing Hermes `mithril_code` tool and `hermes mithril-code status|run --stdin` CLI generate **Mithril language** applications through the fixed Code service and `api.mithril.fund`. One bounded proposal chooses static dashboard, report or directory content. The host emits inert `application.mith` Form source and calls the actual existing Mithril App compiler at `https://app.mithril.fund/api/compile`. The receipt carries OWL, SPARQL, SHACL, compile and conformance stages plus the canonical graph digest. Source, admitted artifact and static HTML are returned for review.

`MITHRIL_API_KEY` is resolved under the owning Hermes profile's secret scope. Credentials never enter the compiler, generated files or GitHub payloads. There is no OpenRouter client, shell, arbitrary repository execution, automatic retry, file write or publication. Normal Mithril inference and Code allowances apply. Non-static application logic remains outside this bounded contract. The historical CLJK To-do verifier is a separate service template and is not a Mithril compiler qualification.

Tools take effect in a new conversation; existing conversation prompts and toolsets are preserved. Keep GitHub saves and Pages publication as separate reviewed Desktop actions. The Desktop shared chat consumes the same tool results and opens the common source editor.

```sh
printf '%s' '{"goal":"Create a static ontology report in Mithril"}' | hermes mithril-code run --stdin
```

Validation uses real plugin discovery, isolated A → B → A profile secret scopes, the actual HTTP adapter and CLI. A synthetic test receipt does not establish a live model or compiler result.
