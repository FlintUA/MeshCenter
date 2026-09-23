"""Regression coverage (Radio TCP Transport, part 2's own explicit
requirement): MCAttach continues to consume only the neutral RadioTransport
interface (meshsrv.radio_transport) - selecting TCP as the active
transport, or MeshtasticTextAdapter simply reporting ConnectionType.TCP
via get_connection_info(), must never introduce a direct dependency on
adapters.meshtastic.tcp_transport.TCPTransport (or the meshtastic package
itself) into meshsrv.attachments.

Two complementary checks: (1) a live functional check - MeshtasticTextAdapter
works correctly against a fake transport reporting ConnectionType.TCP,
using nothing but the neutral models; (2) a static import-graph check
mirroring CI's own GPLv3 license-boundary grep (.github/workflows/ci.yml) -
every module actually imported by meshsrv.attachments must contain no
`import meshtastic`/`from meshtastic` line and no `adapters.meshtastic`
reference, run here as a local, fast unit test so a future regression is
caught before CI, not only by it (that CI step already covers the whole
repo, including this tree, on every PR - this is a faster, more
narrowly-scoped local mirror of the same check, not a replacement for it).
"""
import ast
import importlib
import pkgutil
from pathlib import Path

import meshsrv.attachments
from meshsrv.attachments.delivery.fakes import FakeRadioTransport, InMemoryEther
from meshsrv.attachments.delivery.meshtastic import MeshtasticTextAdapter
from meshsrv.radio_transport import ConnectionState, ConnectionType


def test_meshtastic_text_adapter_works_against_a_tcp_reporting_fake_transport():
    """Purely functional: MeshtasticTextAdapter never branches on the
    concrete transport CLASS, only on the neutral ConnectionType/
    ConnectionState values get_connection_info() returns - a fake
    reporting ConnectionType.TCP is handled identically to serial/BLE."""
    ether = InMemoryEther()
    transport = FakeRadioTransport(
        ether, "!1fa065f0", connection_type=ConnectionType.TCP, connection_state=ConnectionState.CONNECTED
    )
    adapter = MeshtasticTextAdapter(transport, control_channel_index=0)

    caps = adapter.capabilities()
    assert caps.connector_state.name == "READY"

    route = adapter.resolve_route({"node_id": "!bbbbbbbb"})
    assert route.destination_address == "!bbbbbbbb"


def _iter_meshsrv_attachments_module_names():
    """Every module under meshsrv.attachments, real import names (not
    filesystem paths) - mirrors how Python itself would resolve them,
    so this check exercises the actual import graph, not a filesystem
    approximation of it."""
    package = meshsrv.attachments
    prefix = package.__name__ + "."
    names = [package.__name__]
    for module_info in pkgutil.walk_packages(package.__path__, prefix):
        names.append(module_info.name)
    return names


def test_no_meshsrv_attachments_module_imports_meshtastic_or_adapters_meshtastic():
    """Static check, mirroring .github/workflows/ci.yml's own GPLv3
    license-boundary grep step (anchored per-line, so even a function-
    local lazy import would still be caught) - but scoped to exactly the
    module tree this feature touches, and run as a fast local unit test
    rather than only relying on the repo-wide CI step to catch a
    regression here."""
    violations = []
    for module_name in _iter_meshsrv_attachments_module_names():
        spec = importlib.util.find_spec(module_name)
        if spec is None or spec.origin is None or not spec.origin.endswith(".py"):
            continue
        source = Path(spec.origin).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=spec.origin)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "meshtastic" or alias.name.startswith("meshtastic.") \
                            or alias.name == "adapters.meshtastic" or alias.name.startswith("adapters.meshtastic."):
                        violations.append(f"{module_name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "meshtastic" or module.startswith("meshtastic.") \
                        or module == "adapters.meshtastic" or module.startswith("adapters.meshtastic."):
                    violations.append(f"{module_name}: from {module} import ...")

    assert violations == [], (
        "meshsrv.attachments must never import meshtastic or adapters.meshtastic directly - "
        f"found: {violations}"
    )
