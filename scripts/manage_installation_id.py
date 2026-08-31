#!/usr/bin/env python3
"""Inspect or regenerate this MeshCenter installation's identifier.

Stage F (final) of the installation-ID rollout - see meshsrv/instance_manager.py
(schema v2's "installation" block) and meshsrv/installation_identity.py (the
generator/validator this script calls directly, the same one server.py uses).

This script never imports server.py - it constructs its own InstanceManager,
same DI-everywhere pattern every other consumer in this codebase follows, and
avoids triggering server.py's whole module-level startup sequence (radio
detection, Meshtastic CLI resolution, etc.), which would be entirely wrong
for a standalone CLI tool.

CRITICAL: InstanceManager caches in memory - a running meshcenter.service
process holds the old id in memory and keeps serving it via /api/instance and
the "MeshCenter Instance" UI card until restarted, even after this script has
already changed the file on disk. `regenerate` checks systemctl is-active and
refuses to proceed by default while the service is running (see --force).

Core logic (show_installation/regenerate_installation) takes an InstanceManager
and, for regenerate, injected is_service_active/confirm callables - callers
(the cmd_* argparse glue below, or a test) supply the side-effecting bits
directly rather than this module reaching for systemctl/input() itself.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from meshsrv.installation_identity import generate_installation_id  # noqa: E402
from meshsrv.instance_manager import InstanceManager  # noqa: E402

SERVICE_NAME = "meshcenter.service"


def instance_file_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "instance.json"


def is_service_active(service_name: str = SERVICE_NAME) -> bool:
    """True if systemd reports the service as currently running. False on
    any failure to determine this (systemctl missing, not under systemd,
    etc.) - a script running outside a real deployment (e.g. a dev machine)
    should not be blocked by an environment that has no such service at all."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return False
    return result.returncode == 0 and result.stdout.strip() == "active"


def show_installation(manager: InstanceManager) -> str:
    installation = manager.load_or_create({}).get("installation", {})
    return (
        f"Installation ID:   {installation.get('id', '(none)')}\n"
        f"Assigned at:       {installation.get('assigned_at') or '(not yet confirmed)'}\n"
        f"Time source:       {installation.get('time_source', 'pending')}\n"
        f"Assignment reason: {installation.get('assignment_reason', '(unknown)')}"
    )


def regenerate_installation(
    manager: InstanceManager,
    is_service_active_fn: Callable[[], bool],
    confirm_fn: Callable[[str], bool],
    force: bool = False,
    assume_yes: bool = False,
) -> tuple[int, str]:
    """Returns (exit_code, message). exit_code 0 means the ID was changed;
    1 means refused/aborted with no changes made. Never calls systemctl or
    input() directly - those come in as is_service_active_fn/confirm_fn so
    this function is fully testable without touching the real system."""
    identity = manager.load_or_create({})
    old_id = identity.get("installation", {}).get("id", "(none)")
    lines = [f"Current Installation ID: {old_id}"]

    if is_service_active_fn():
        if not force:
            lines.append(
                f"\n{SERVICE_NAME} is currently running. Regenerating now would change "
                "the file on disk, but the running process caches the old ID in memory "
                "and would keep serving it via /api/instance and the System card until "
                "restarted - the UI and the file would silently disagree.\n\n"
                f"Stop the service first:\n  sudo systemctl stop {SERVICE_NAME}\n"
                "then run this command again, or pass --force to proceed anyway."
            )
            return 1, "\n".join(lines)
        lines.append(
            f"\nWARNING: {SERVICE_NAME} is running and --force was given - proceeding "
            "anyway. The running process will keep showing the OLD ID until you restart "
            f"it: sudo systemctl restart {SERVICE_NAME}"
        )

    if not assume_yes and not confirm_fn(old_id):
        lines.append("\nAborted - no changes made.")
        return 1, "\n".join(lines)

    updated = dict(identity)
    updated["installation"] = {
        "id": generate_installation_id(),
        "assigned_at": None,
        "time_source": "pending",
        "assignment_reason": "regeneration",
    }
    new_identity = manager.save(updated)
    new_id = new_identity["installation"]["id"]

    lines.append(f"\nNew Installation ID: {new_id}")
    lines.append(f"Restart the service for this to take effect: sudo systemctl restart {SERVICE_NAME}")
    return 0, "\n".join(lines)


def _prompt_confirm(old_id: str) -> bool:
    answer = input(
        f"\nThis will permanently invalidate Installation ID {old_id} for any "
        "external correlation. Continue? [y/N] "
    ).strip().lower()
    return answer in ("y", "yes")


def cmd_show(args: argparse.Namespace, manager: InstanceManager) -> int:
    print(show_installation(manager))
    return 0


def cmd_regenerate(args: argparse.Namespace, manager: InstanceManager) -> int:
    exit_code, message = regenerate_installation(
        manager,
        is_service_active_fn=is_service_active,
        confirm_fn=_prompt_confirm,
        force=args.force,
        assume_yes=args.yes,
    )
    print(message, file=sys.stderr if exit_code else sys.stdout)
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or regenerate this MeshCenter installation's identifier.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    show_parser = subparsers.add_parser("show", help="Print the current installation identity (read-only).")
    show_parser.set_defaults(func=cmd_show)

    regen_parser = subparsers.add_parser("regenerate", help="Generate a new installation ID, replacing the current one.")
    regen_parser.add_argument("-y", "--yes", action="store_true", help="Skip the interactive confirmation prompt.")
    regen_parser.add_argument(
        "--force", action="store_true",
        help=f"Proceed even if {SERVICE_NAME} is currently running (not recommended - see warning).",
    )
    regen_parser.set_defaults(func=cmd_regenerate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)  # --help/-h exits here, before config.py is needed

    from config import DATA_DIR  # noqa: E402 - deferred: only a real command needs config.py present

    manager = InstanceManager(instance_file_path(DATA_DIR))
    return args.func(args, manager)


if __name__ == "__main__":
    sys.exit(main())
