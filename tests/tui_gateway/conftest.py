"""tui_gateway test fixtures.

Several files here import ``tui_gateway.server`` inside a ``patch.dict("sys.modules", {"hermes_constants":
MagicMock(...)})`` window so the module binds a fixed home. The server's import graph reaches
``agent.process_bootstrap`` → ``hermes_bootstrap``, which is process boot: PM dependency activation reads
the real install root through ``hermes_constants`` and exits the process when that is a MagicMock.
Importing it once here, before any window opens, keeps boot out of the mocked import.
"""

import hermes_bootstrap  # noqa: F401


def pytest_addoption(parser):
    parser.addoption("--owned-sdk-module", default=None,
                     help="Path to the already-built Mithril owned gateway SDK module")
    parser.addoption("--owned-dashboard-adapter", default=None,
                     help="Optional built Desktop target-preview adapter for real stdio/WebSocket qualification")
    parser.addoption("--approval-ui-web-client", default=None,
                     help="Web production DashboardGatewayClient source for local mounted qualification")
    parser.addoption("--approval-ui-desktop-client", default=None,
                     help="Desktop production DashboardGatewayClient source for local mounted qualification")
    parser.addoption("--approval-ui-sdk-version", default="0.6.25-agency.12",
                     help="Exact expected SDK package version for mounted qualification; legacy default preserved")

    parser.addoption("--owned-browser-fund-root", default=None,
                     help="Fund checkout with built Kuro assets and dependencies for opt-in real Browser/API/Hermes qualification")
    parser.addoption("--owned-browser-executable", default=None,
                     help="Optional installed Chromium executable for the real Browser qualification")

    parser.addoption("--owned-desktop-main-module", default=None,
                     help="Optional built Desktop CloudChat/CloudWorkspace/native queue for real HTTP consent qualification")

    parser.addoption("--owned-desktop-chat-source", default=None,
                     help="Optional actual MithrilChat source for real native Chat WASM/HTTP/Web-consent qualification")

    parser.addoption("--owned-desktop-electron-main", default=None,
                     help="Opt-in built real Electron qualification main entry")
    parser.addoption("--owned-desktop-electron-preload", default=None,
                     help="Actual built production Desktop preload for Electron qualification")
    parser.addoption("--owned-desktop-electron-executable", default=None,
                     help="Explicit Electron executable; no install or installed app mutation")

    parser.addoption("--owned-qualification-output", default=None,
                     help="Optional absolute task directory for bounded qualifier stdout/stderr evidence")
