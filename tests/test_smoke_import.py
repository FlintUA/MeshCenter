"""Sanity check that server.py imports cleanly under the test sandbox (see
tests/conftest.py). Not testing behavior - just that the import shim works,
so a broken conftest fails fast and obviously instead of masquerading as
unrelated failures in every other test file.
"""


def test_server_module_imports(server_module):
    assert server_module.LOCAL_NODE_ID == "!aabbccdd"
    assert callable(server_module.sanitize_text)
