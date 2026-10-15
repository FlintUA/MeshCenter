"""DK2 container/UI consistency fixes (non-TCP-recovery part)."""
import io
import threading
from pathlib import Path

from meshsrv import line_atomic_stdout, runtime_env
from meshsrv.adapter_ipc_client import AdapterIPCTransport
from meshsrv.radio_transport import ConnectionDescriptor, ConnectionInfo, ConnectionState, ConnectionType

REPO = Path(__file__).resolve().parent.parent


# ---- MCA control channel ---------------------------------------------------

def test_container_config_template_documents_the_mca_channel_explicitly():
    template = (REPO / "deploy/docker/config.container.py").read_text(encoding="utf-8")
    assert "MCA_CONTROL_CHANNEL_INDEX = None" in template, "no invented default"
    assert "private" in template and "REQUIRED" in template and "fail-closed" in template
    docs = (REPO / "docs/Docker_Installation.md").read_text(encoding="utf-8")
    assert "MCAttach: set your private channel index" in docs


def test_startup_warns_when_the_mca_channel_is_unset():
    source = (REPO / "server.py").read_text(encoding="utf-8")
    assert "if MCA_CONTROL_CHANNEL_INDEX is None:" in source
    assert "MCAttach control channel not configured" in source
    assert source.index("if MCA_CONTROL_CHANNEL_INDEX is None:") < source.index("mca_runtime.start_attachments_service(")


# ---- i18n notice -----------------------------------------------------------

def test_container_notice_uses_data_i18n_not_an_early_t_call():
    html = (REPO / "templates/index.html").read_text(encoding="utf-8")
    assert 'id="containerModeNotice"' in html and 'data-i18n="system.container_mode_notice"' in html
    js = (REPO / "static/runtime-caps.js").read_text(encoding="utf-8")
    code = " ".join(line for line in js.splitlines() if not line.strip().startswith("//"))
    assert "I18N.t(" not in code, "runtime-caps.js runs before the catalogs load"


# ---- capabilities / footer -------------------------------------------------

def test_cpu_temp_capability_follows_container_mode(monkeypatch):
    monkeypatch.delenv("MESHCENTER_RUNTIME", raising=False)
    assert runtime_env.capabilities()["has_cpu_temp"] is True
    monkeypatch.setenv("MESHCENTER_RUNTIME", "container")
    assert runtime_env.capabilities()["has_cpu_temp"] is False
    js = (REPO / "static/chat.js").read_text(encoding="utf-8")
    assert "MC_RUNTIME?.has_cpu_temp" in js
    assert "MC_RUNTIME?.hardware_io" in js


# ---- placeholder -----------------------------------------------------------

def test_untouched_placeholder_is_pruned_once_a_real_radio_is_accepted(server_module, monkeypatch):
    s = server_module
    monkeypatch.setattr(s, "LOCAL_NODE_ID", "!1fa065f0")
    monkeypatch.setattr(s, "save_nodes", lambda: None)
    monkeypatch.setattr(s, "save_chats", lambda: None)
    with s.state_lock:
        s.nodes["!00000000"] = {"node_id": "!00000000", "name": "My Node", "last_seen": 0}
        s.chats["!00000000"] = {"name": "My Node", "last_message": "", "unread": 0}
    try:
        s.prune_placeholder_node()
        assert "!00000000" not in s.nodes and "!00000000" not in s.chats
    finally:
        s.nodes.pop("!00000000", None)
        s.chats.pop("!00000000", None)


def test_placeholder_with_real_data_or_active_is_never_removed(server_module, monkeypatch):
    s = server_module
    monkeypatch.setattr(s, "save_nodes", lambda: None)
    monkeypatch.setattr(s, "save_chats", lambda: None)
    try:
        monkeypatch.setattr(s, "LOCAL_NODE_ID", "!1fa065f0")
        with s.state_lock:
            s.nodes["!00000000"] = {"node_id": "!00000000", "last_seen": 1790000000}
            s.chats["!00000000"] = {"name": "x", "last_message": "hello", "unread": 0}
        s.prune_placeholder_node()
        assert "!00000000" in s.nodes and "!00000000" in s.chats, "user data stays"

        monkeypatch.setattr(s, "LOCAL_NODE_ID", "!00000000")
        with s.state_lock:
            s.nodes["!00000000"] = {"node_id": "!00000000", "last_seen": 0}
        s.prune_placeholder_node()
        assert "!00000000" in s.nodes, "placeholder still IS the accepted radio"
    finally:
        s.nodes.pop("!00000000", None)
        s.chats.pop("!00000000", None)


# ---- banner / version ------------------------------------------------------

def test_link_label_shows_the_tcp_endpoint_not_a_serial_leftover(server_module):
    s = server_module
    tcp = {"transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403}, "port": "/dev/ttyACM0"}
    assert s._radio_link_label(tcp, "/dev/ttyACM0", prefix="Endpoint: ", port_prefix="Port: ") == "Endpoint: tcp 192.168.2.34:4403"
    serial = {"transport": "serial", "port": "/dev/ttyACM0"}
    assert s._radio_link_label(serial, "/dev/x", prefix="Endpoint: ", port_prefix="Port: ") == "Port: /dev/ttyACM0"


def test_version_skips_git_in_a_container_with_a_baked_version(server_module, monkeypatch):
    s = server_module
    monkeypatch.setenv("MESHCENTER_RUNTIME", "container")
    monkeypatch.setenv("MESHCENTER_VERSION", "v9.9.9")

    def boom(*a, **k):
        raise AssertionError("git must not run")

    monkeypatch.setattr(s.subprocess, "run", boom)
    assert s.resolve_app_version("/nonexistent") == "9.9.9"


def test_camera_is_one_info_line_in_a_container(monkeypatch, capsys):
    from camera import camera
    monkeypatch.setenv("MESHCENTER_RUNTIME", "container")
    assert camera.init_camera() is False
    out = capsys.readouterr().out
    assert out.strip().splitlines() == ["[CAMERA] INFO: camera disabled in container"]


# ---- stdout interleaving ---------------------------------------------------

def test_line_atomic_stdout_never_splits_a_line_between_threads():
    sink = io.StringIO()
    writer = line_atomic_stdout.LineAtomicWriter(sink)
    barrier = threading.Barrier(2)

    def worker(tag):
        barrier.wait()
        for i in range(300):
            print(f"{tag}-line-{i}-end", file=writer, flush=True)

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("A", "B")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    lines = sink.getvalue().splitlines()
    assert len(lines) == 600
    assert all(line.endswith("-end") and line[0] in "AB" and line.count("-line-") == 1 for line in lines)


# ---- router: never refreshes a serial IPC transport -------------------------

def test_router_refresh_never_calls_a_serial_ipc_transport():
    from meshsrv.transport_router import TransportRouter

    class Boom:
        def call(self, *a, **k):
            raise AssertionError("no IPC (and so no serial-port claim) for a serial refresh")

    serial = AdapterIPCTransport(ConnectionType.SERIAL, Boom())
    router = TransportRouter(serial)
    assert router.refresh_connection_info() is False

    class FakeTcp:
        refreshed = 0
        transport_type = ConnectionType.TCP

        def refresh_connection_info(self, timeout=5.0):
            self.refreshed += 1

    tcp = FakeTcp()
    assert TransportRouter(tcp).refresh_connection_info() is True and tcp.refreshed == 1


# ---- DK3: version label --------------------------------------------------------

def test_a_non_version_label_never_claims_an_update_is_available():
    from meshsrv import update_service
    cache = {"latest_version": "1.10.0", "check_ok": True}
    assert update_service.update_available("dk2", cache) is False      # the NAS false positive
    assert update_service.update_available("dev", cache) is False
    assert update_service.update_available("unknown", cache) is False
    assert update_service.update_available("1.10.0-27-gabc1234", cache) is False   # ahead of a tag
    assert update_service.update_available("1.9.0", cache) is True                  # genuinely behind
    assert update_service.update_available("1.10.0", cache) is False


def test_build_script_labels_the_image_with_git_describe():
    script = (REPO / "deploy/docker/build.sh").read_text(encoding="utf-8")
    assert "git describe --tags --always" in script and "MESHCENTER_VERSION=" in script
