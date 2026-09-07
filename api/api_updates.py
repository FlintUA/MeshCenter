import subprocess
import threading
import time

from flask import jsonify

from meshsrv import update_service


def register_updates_routes(app, resolve_version, project_dir, handle_errors):
    def _restart_after_update():
        """Same self-restart pattern as api_system.py's execute_system_action
        for 'restart_meshcenter': sleep briefly so the HTTP response for
        this request has time to reach the client before the process is
        killed, then restart via the same narrowly-scoped NOPASSWD sudo
        rule (deploy/meshcenter.sudoers)."""
        time.sleep(1.0)
        try:
            subprocess.run(
                ["sudo", "-n", "/usr/bin/systemctl", "restart", "meshcenter.service"],
                check=True, capture_output=True, text=True, timeout=20,
            )
        except Exception as error:
            print(f"[UPDATES] Restart after update failed: {error}", flush=True)

    @app.route("/api/updates/status")
    @handle_errors
    def api_updates_status():
        return jsonify({"ok": True, **update_service.get_status(resolve_version())})

    @app.route("/api/updates/check", methods=["POST"])
    @handle_errors
    def api_updates_check():
        return jsonify({"ok": True, **update_service.check_now(resolve_version())})

    @app.route("/api/updates/preflight")
    @handle_errors
    def api_updates_preflight():
        return jsonify(update_service.git_preflight(project_dir))

    @app.route("/api/updates/apply", methods=["POST"])
    @handle_errors
    def api_updates_apply():
        preflight = update_service.git_preflight(project_dir)
        if not preflight.get("ok"):
            return jsonify({
                "ok": False,
                "error": "Preflight check failed",
                "preflight": preflight,
            }), 409

        result = update_service.apply_update(project_dir, preflight["upstream"])
        if not result.get("ok"):
            return jsonify({
                "ok": False,
                "error": "git merge failed",
                "detail": result,
            }), 500

        requirements_changed = result.get("requirements_changed", False)
        if requirements_changed:
            # PR #231 review (2nd pass): restarting automatically here
            # would very likely crash-loop the service - the code on disk
            # now expects a dependency that was never installed, and
            # apply_update() deliberately never runs pip itself (see its
            # own comment on why). Skip the auto-restart; the operator is
            # told to install dependencies and restart manually, the same
            # instruction the log line already gave, now also carried in
            # the response so a caller doesn't have to go read the
            # server's own console output to learn a restart is needed.
            print(
                "[UPDATES] requirements.txt changed as part of this update - skipping the "
                "automatic restart below. Run `source venv/bin/activate && "
                "pip install -r requirements.txt`, then restart meshcenter.service manually.",
                flush=True,
            )
        else:
            threading.Thread(target=_restart_after_update, daemon=True).start()

        return jsonify({
            "ok": True,
            "previous_sha": result["previous_sha"],
            "requirements_changed": requirements_changed,
            "restarted": not requirements_changed,
        }), 202
