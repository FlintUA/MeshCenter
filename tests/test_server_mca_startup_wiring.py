"""tests/test_server_mca_startup_wiring.py

Regression coverage for a PR #231 review defect (section 3):
mca_runtime.start_attachments_service() used to be called only inside
server.py's `if identity_match:` block, meaning AttachmentsService never
started at all - and every previously-drafted/in-flight attachment stayed
frozen - for as long as the connected radio's identity was unconfirmed or
mismatched, even though that has nothing to do with whether already-queued
attachment work can advance (see server.py's own comment at the fixed call
site, and ensure_service()'s "restart never loses a job" guarantee).

This does not call start_runtime() itself - no test in this suite does
(see test_smoke_import.py's own docstring: it starts real background
threads/radio connections) - it is a static, AST-level check that the
mca_runtime.start_attachments_service(...) call inside start_runtime()'s
function body is not nested under any `if`/`else` at all, the same class
of "guard against a stale-branch wiring regression" check
scripts/check_startup_calls.py already performs for the other startup
calls in this same function.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in server.py")


def _is_start_attachments_service_call(stmt: ast.stmt) -> bool:
    if not isinstance(stmt, ast.Try):
        return False
    for inner in stmt.body:
        if not isinstance(inner, ast.Expr) or not isinstance(inner.value, ast.Call):
            continue
        call = inner.value
        if (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "start_attachments_service"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "mca_runtime"
        ):
            return True
    return False


def _contains_listen_meshtastic_thread_start(node: ast.AST) -> bool:
    """True if `node` contains `threading.Thread(target=listen_meshtastic,
    ...)` anywhere within it (a call expression, at any nesting depth) -
    used both to locate the top-level `if identity_match:` statement that
    contains it and, in the ordering test below, to make sure the two
    "found" indices actually refer to the statements this test thinks
    they do."""
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call):
            continue
        if not (isinstance(inner.func, ast.Attribute) and inner.func.attr == "Thread"):
            continue
        for kw in inner.keywords:
            if kw.arg == "target" and isinstance(kw.value, ast.Name) and kw.value.id == "listen_meshtastic":
                return True
    return False


def test_start_attachments_service_is_not_gated_on_identity_match():
    source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="server.py")
    start_runtime = _find_function(tree, "start_runtime")

    # Top-level statements only (not ast.walk()) - the whole point is that
    # this call must be a direct statement in start_runtime()'s own body,
    # not nested inside any `if`/`else`/`try` guarded by identity_match.
    matches = [stmt for stmt in start_runtime.body if _is_start_attachments_service_call(stmt)]
    assert len(matches) == 1, (
        "expected exactly one top-level "
        "`try: mca_runtime.start_attachments_service(...)` statement directly in "
        "start_runtime()'s body (not nested inside an `if identity_match:` block) - "
        f"found {len(matches)}. If this call has been moved back inside a "
        "conditional, AttachmentsService will stop starting whenever the radio "
        "identity is unconfirmed/mismatched, silently freezing every pending "
        "MCAttach transfer until that's resolved."
    )

    # Belt-and-braces: also confirm no `if`/`elif` node in the whole
    # function body contains the call as a descendant - catches the call
    # being wrapped in a *nested* conditional several levels down, which
    # the top-level-statement check above would not by itself rule out.
    for node in ast.walk(start_runtime):
        if isinstance(node, (ast.If,)):
            for inner in ast.walk(node):
                if _is_start_attachments_service_call(inner):
                    raise AssertionError(
                        "mca_runtime.start_attachments_service(...) is called from inside "
                        "an `if`/`elif` block in start_runtime() - it must be unconditional "
                        "(see this test's module docstring)."
                    )


def test_start_attachments_service_completes_before_listen_meshtastic_starts():
    """PR #231 review (2nd pass), requirement 6: the previous test above
    only proves start_attachments_service() is unconditional - it says
    nothing about *when*, relative to listen_meshtastic, it runs. Actual
    ORDER matters here, not just presence: handle_incoming_meshtastic_
    text() (called only from the radio listener's own thread, started by
    that `threading.Thread(target=listen_meshtastic, ...)` call) no
    longer performs any initialization of its own (see mca_runtime.py's
    module docstring) - it assumes the runtime, and the bounded queue it
    enqueues onto, already exist. If listen_meshtastic's thread could
    start before start_attachments_service() has run, an inbound message
    could arrive and find no runtime to enqueue into.

    This walks start_runtime()'s own top-level statement list (not a
    full ast.walk() - order is only meaningful between siblings at the
    same nesting level) and asserts the top-level statement containing
    the start_attachments_service() call appears before the top-level
    `if identity_match:` statement that starts listen_meshtastic's
    thread."""
    source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="server.py")
    start_runtime = _find_function(tree, "start_runtime")

    start_attachments_index = None
    listen_meshtastic_index = None
    for index, stmt in enumerate(start_runtime.body):
        if start_attachments_index is None and _is_start_attachments_service_call(stmt):
            start_attachments_index = index
        if listen_meshtastic_index is None and _contains_listen_meshtastic_thread_start(stmt):
            listen_meshtastic_index = index

    assert start_attachments_index is not None, "mca_runtime.start_attachments_service(...) call not found"
    assert listen_meshtastic_index is not None, "threading.Thread(target=listen_meshtastic, ...) call not found"
    assert start_attachments_index < listen_meshtastic_index, (
        f"mca_runtime.start_attachments_service(...) (top-level statement #{start_attachments_index}) "
        f"must appear BEFORE the threading.Thread(target=listen_meshtastic, ...) statement "
        f"(#{listen_meshtastic_index}) in start_runtime() - the radio listener must never be able "
        "to start before the MCA runtime (and its inbound queue) is ready, since handle_incoming_"
        "meshtastic_text() no longer initializes anything itself and will just drop an inbound "
        "event if the runtime isn't there yet."
    )
