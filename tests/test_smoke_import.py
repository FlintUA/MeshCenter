"""Sanity check that server.py imports cleanly under the test sandbox (see
tests/conftest.py). Not testing behavior - just that the import shim works,
so a broken conftest fails fast and obviously instead of masquerading as
unrelated failures in every other test file.
"""


def test_server_module_imports(server_module):
    assert server_module.LOCAL_NODE_ID == "!aabbccdd"
    assert callable(server_module.sanitize_text)


def test_start_runtime_exists_but_is_not_invoked_by_import(server_module):
    # start_runtime() starts real background threads (radio listener,
    # telemetry workers, ...) and must never run just because server.py was
    # imported - that's the whole point of extracting it out of
    # `if __name__ == "__main__":` (see wsgi.py, which calls it explicitly).
    # This only checks it exists and hasn't run - it deliberately does NOT
    # call it, since that would actually spin up those threads/radio
    # attempts against the sandboxed fake CLI from conftest.py.
    assert callable(server_module.start_runtime)
    assert server_module._runtime_started is False
