# Hermes Code integration — 2026-10-06

The existing Code runner is connected through the owned Mithril plugin, using Hermes's real plugin discovery, scoped tool registry and CLI command registration. Two tests pass using the required isolated runner (`scripts/run_tests.sh`), including two temporary profile homes A→B→A under multiplex, actual authenticated loopback HTTP, actual `hermes mithril-code run --stdin` execution, fixed todo payload, redirect refusal and rejection of unverified results. The service responses are explicitly synthetic adapter fixtures; no live model call or production profile configuration was used.

The original installed Python runtime caused the test home-I/O guard to reject stdlib reads inside the real Hermes home. A fresh disposable PM test environment outside that home corrected test isolation; no user runtime was modified.

Fund PR492's closeout was inspected at head965f6f8a94c3cb486e09222e5e34beb903da80b1. It remains open and conflicting with main; articles, video and registered free pilot are unpublished. This work does not merge that PR or alter its quotas/replay/authentication. The separate operator adapter requires explicit runner authority and uses the configured runner's model budget.

Desktop source adds a Code screen and IPC adapter invoking this CLI. Publication of both repositories and the Desktop installer must be verified separately. No claim of fresh Jev inference, registered-account trial qualification, or general repository coding is made.
