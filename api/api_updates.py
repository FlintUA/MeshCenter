import threading
import time

from flask import jsonify

from meshsrv import planned_restart, runtime_env, update_service


def register_updates_routes(app, resolve_version, project_dir, handle_errors, log_event=None):
    """`log_event(title, level, details, source=...)` is system_log.log_system_event: every
    update/rollback start, refusal and failure lands in the System Log (UPD1). The matching
    "finished"/"failed" after the restart is written by the NEW process at boot
    (update_service.reconcile_pending)."""
    log = log_event or (lambda *args, **kwargs: None)

    def _restart_after_update(label):
        """Same self-restart pattern as api_system.py's execute_system_action for
        'restart_meshcenter': sleep briefly so the HTTP response for this request has time to
        reach the client before the process is killed, then restart via the same narrowly-scoped
        NOPASSWD sudo rule (deploy/meshcenter.sudoers). The helper dying with SIGTERM is the
        restart working, not a failure (planned_restart.run_restart_command)."""
        time.sleep(1.0)
        planned_restart.run_restart_command(
            ["sudo", "-n", "/usr/bin/systemctl", "restart", "meshcenter.service"],
            label=label, log=log, source="update",
        )

    def _target_tag():
        return update_service.get_status(resolve_version()).get("latest_tag")

    def _short(sha):
        return str(sha or "")[:7]

    def _manual_note(result):
        steps = result.get("manual_steps") or []
        return f"; manual step needed after the restart: {', '.join(step['file'] for step in steps)}" if steps else ""

    def _blocked_response(result):
        print(
            f"[UPDATES] blocked: requirements changed "
            f"({', '.join(result.get('changed_requirements_files', []))}) - nothing applied, no restart scheduled.",
            flush=True,
        )
        return jsonify({
            "ok": False,
            "blocked": True,
            "error": "Dependency files changed",
            "requirements_changed": True,
            "changed_requirements_files": result.get("changed_requirements_files", []),
            "instructions": result.get("instructions", ""),
            "restarted": False,
        }), 409

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
        if not runtime_env.capabilities()["can_self_update"]:
            return jsonify(runtime_env.refusal("can_self_update")), 403
        return jsonify(update_service.git_preflight(project_dir, _target_tag()))

    @app.route("/api/updates/apply", methods=["POST"])
    @handle_errors
    def api_updates_apply():
        if not runtime_env.capabilities()["can_self_update"]:
            return jsonify(runtime_env.refusal("can_self_update")), 403
        current = resolve_version()
        preflight = update_service.git_preflight(project_dir, _target_tag())
        if not preflight.get("ok"):
            log("Update failed", "WARNING",
                f"not applied: {preflight.get('reason') or 'preflight failed'}"
                f"{' (' + str(preflight.get('branch')) + ')' if preflight.get('reason') == 'unsupported_branch' else ''}",
                source="update")
            return jsonify({
                "ok": False,
                "error": "Preflight check failed",
                "preflight": preflight,
            }), 409

        result = update_service.apply_update(project_dir, preflight, current)

        # PR #231 review (3rd pass): apply_update() refuses to move anything when
        # requirements.txt/adapters/meshtastic/requirements.txt changed - HEAD and the working
        # tree are left completely unchanged. A distinct, non-error "blocked" outcome the
        # frontend must surface clearly - never an automatic restart, since there is nothing
        # new to restart into yet.
        if result.get("blocked"):
            log("Update failed", "WARNING",
                f"{current} -> {preflight.get('target_tag')} blocked: dependency files changed "
                f"({', '.join(result.get('changed_requirements_files', []))}); nothing was applied",
                source="update")
            return _blocked_response(result)

        if not result.get("ok"):
            log("Update failed", "ERROR",
                f"{current} -> {preflight.get('target_tag')}: git step failed: {str(result.get('output'))[:300]}",
                source="update")
            return jsonify({
                "ok": False,
                "error": "git update failed",
                "detail": result,
            }), 500

        log("Update started", "ACTION",
            f"{current} -> {result['to_version']} (tag {result['target_tag']} at {_short(result['target_sha'])}; "
            f"previous commit {_short(result['previous_sha'])})" + _manual_note(result),
            source="update")
        planned_restart.begin("update")
        threading.Thread(target=_restart_after_update, args=("Update restart",), daemon=True).start()

        return jsonify({
            "ok": True,
            "blocked": False,
            "previous_sha": result["previous_sha"],
            "previous_version": result.get("previous_version"),
            "to_version": result["to_version"],
            "requirements_changed": False,
            "manual_steps": result.get("manual_steps") or [],
            "restarted": True,
        }), 202

    @app.route("/api/updates/rollback", methods=["POST"])
    @handle_errors
    def api_updates_rollback():
        if not runtime_env.capabilities()["can_self_update"]:
            return jsonify(runtime_env.refusal("can_self_update")), 403
        current = resolve_version()
        preflight = update_service.rollback_preflight(project_dir, current)
        if preflight.get("reason") == "vault_enabled":
            log("Rollback refused", "WARNING", "An MCAttach vault exists; disable it first (older versions cannot read it)", source="update")
            return jsonify({
                "ok": False,
                "error": "Disable the vault first",
                "error_code": "vault_enabled",
                "preflight": preflight,
            }), 409
        if not preflight.get("ok"):
            log("Rollback failed", "WARNING", f"not applied: {preflight.get('reason') or 'preflight failed'}", source="update")
            return jsonify({
                "ok": False,
                "error": "Rollback not possible",
                "preflight": preflight,
            }), 409

        result = update_service.rollback(project_dir, preflight, current)

        if result.get("blocked"):
            log("Rollback failed", "WARNING",
                f"{current} -> {preflight.get('previous_version')} blocked: dependency files changed "
                f"({', '.join(result.get('changed_requirements_files', []))}); nothing was applied",
                source="update")
            return _blocked_response(result)

        if not result.get("ok"):
            log("Rollback failed", "ERROR",
                f"{current} -> {preflight.get('previous_version')}: git step failed: {str(result.get('output'))[:300]}",
                source="update")
            return jsonify({"ok": False, "error": "git rollback failed", "detail": result}), 500

        log("Rollback started", "ACTION",
            f"{current} -> {result['to_version']} (commit {_short(preflight.get('previous_sha'))})" + _manual_note(result),
            source="update")
        planned_restart.begin("rollback")
        threading.Thread(target=_restart_after_update, args=("Rollback restart",), daemon=True).start()

        return jsonify({
            "ok": True,
            "blocked": False,
            "to_version": result["to_version"],
            "manual_steps": result.get("manual_steps") or [],
            "restarted": True,
        }), 202
