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
    parser.addoption("--approval-ui-web-client", default=None,
                     help="Web production DashboardGatewayClient source for local mounted qualification")
    parser.addoption("--approval-ui-desktop-client", default=None,
                     help="Desktop production DashboardGatewayClient source for local mounted qualification")
