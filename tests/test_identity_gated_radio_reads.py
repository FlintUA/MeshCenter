"""Nothing may read the radio's nodes/identity through a TCP/Bluetooth session
except the identity code itself and the one post-MATCH node seed.

radio_health_worker and telemetry must never touch a session that identity
verification has refused (MISMATCH / NOT_FOUND). Rather than trust that by
inspection, this walks the source: every call to get_nodes()/get_local_node()/
get_metadata() in non-test Python must sit in a known function, so a new caller
(e.g. a worker that "just reads the node list") fails here and forces a
deliberate decision about the identity gate.
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCAN_DIRS = ["", "api", "meshsrv", "storage", "telemetry", "modules", "system", "hardware", "camera", "weather"]
SKIP_PARTS = {"venv", "tests", "adapters", "__pycache__", "relay-server", ".theme_stage0_scratch"}
READ_METHODS = {"get_nodes", "get_local_node", "get_metadata"}

# (file, enclosing function) -> why it is allowed to read through a transport.
ALLOWED = {
    # The one node seed, called only from restore_active_transport() after
    # identity_match (test_server_startup_tcp_transport.py pins that gate).
    ("server.py", "seed_nodes_from_transport"): "post-MATCH node seed",
    # Identity detection itself: the read that DECIDES MATCH/MISMATCH.
    ("meshsrv/radio_identity.py", "_tcp_identity_from_connected_transport"): "identity read",
}


def _read_call_sites():
    sites = []
    for directory in SCAN_DIRS:
        base = ROOT / directory if directory else ROOT
        pattern = "*.py" if not directory else "**/*.py"
        for path in base.glob(pattern):
            if SKIP_PARTS & set(path.relative_to(ROOT).parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            relative = path.relative_to(ROOT).as_posix()

            class Visitor(ast.NodeVisitor):
                def __init__(self):
                    self.stack = []

                def visit_FunctionDef(self, node):
                    self.stack.append(node.name)
                    self.generic_visit(node)
                    self.stack.pop()

                visit_AsyncFunctionDef = visit_FunctionDef

                def visit_Call(self, node):
                    func = node.func
                    if isinstance(func, ast.Attribute) and func.attr in READ_METHODS:
                        sites.append((relative, self.stack[-1] if self.stack else "<module>", func.attr))
                    self.generic_visit(node)

            Visitor().visit(tree)
    return sites


def test_every_node_or_identity_read_through_a_transport_is_a_known_gated_site():
    unexpected = [
        site for site in _read_call_sites() if (site[0], site[1]) not in ALLOWED
    ]
    assert unexpected == [], (
        "New caller(s) reading nodes/identity through a radio transport: "
        f"{unexpected}. Decide how each is gated on RADIO_IDENTITY_RESULT (MATCH), then add it to ALLOWED."
    )


def test_the_allowed_sites_still_exist():
    """Guards the whitelist against going stale (a renamed function would
    otherwise silently widen what the audit accepts)."""
    present = {(file, function) for file, function, _ in _read_call_sites()}
    assert set(ALLOWED) <= present, sorted(set(ALLOWED) - present)


def test_health_worker_never_reads_nodes_or_identity(server_module):
    """radio_health_worker only reads connection STATE (in-memory on the
    adapter side), never nodes/identity/metadata."""
    import inspect

    source = inspect.getsource(server_module.radio_health_worker)
    for method in READ_METHODS | {"seed_nodes_from_transport", "send_text", "send_messages", "send_packet"}:
        assert method not in source, f"radio_health_worker references {method}"


def test_seed_nodes_is_only_reachable_after_identity_match(server_module):
    """seed_nodes_from_transport() has exactly one caller, inside
    restore_active_transport(), which returns before it for an unverified
    tcp endpoint."""
    import inspect

    callers = []
    tree = ast.parse(inspect.getsource(server_module))
    for function in ast.walk(tree):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "seed_nodes_from_transport":
                    callers.append(function.name)
    assert set(callers) == {"restore_active_transport"}, callers
    restore = ast.parse(inspect.getsource(server_module.restore_active_transport)).body[0]
    guard_lines = [
        node.lineno
        for node in ast.walk(restore)
        if isinstance(node, ast.If)
        and "identity_match" in ast.unparse(node.test)
        and any(isinstance(child, ast.Return) for child in node.body)
    ]
    seed_lines = [
        node.lineno
        for node in ast.walk(restore)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "seed_nodes_from_transport"
    ]
    assert guard_lines and seed_lines
    assert min(guard_lines) < min(seed_lines), "the identity_match early-return must precede the node seed"
