#!/usr/bin/env bash
# Shared installer logic for install.sh and meshcenter-firstboot.sh.
#
# Both scripts need to turn a physically connected Meshtastic radio's
# `--info` output into a real config.py (LOCAL_NODE_ID/LOCAL_NODE_NAME/
# KNOWN_NODES/KNOWN_NODE_INFO/MESHTASTIC_PORT), instead of shipping the
# `!xxxxxxxx` placeholder from config.example.py. meshcenter-firstboot.sh
# already did this correctly; install.sh used to just `cp
# config.example.py config.py` and leave the placeholder in - see git log
# for the fix that extracted this file.
#
# This file only holds the parts that are identical between the two
# callers (parsing `--info` text, writing+validating config.py). It does
# NOT own the "wait for a radio to appear" loop or its failure policy -
# meshcenter-firstboot.sh waits up to RADIO_WAIT_SECONDS and fails the
# whole install if no radio shows up (automatic/unattended provisioning,
# a radio is expected to be plugged in); install.sh tries once and falls
# back to the config.example.py placeholder with a warning (the manual
# path has always allowed installing before a radio is connected - see
# its step_detect_radio()). That policy difference is exactly the kind of
# thing that shouldn't live in a shared function.
#
# Not meant to be executed directly - source it:
#   # shellcheck source=installer/common.sh
#   source "$(dirname "${BASH_SOURCE[0]}")/installer/common.sh"

# parse_meshtastic_identity_from_info <info_file> <port>
#
# Reads a captured `meshtastic --port <port> --info` transcript and prints
# a single-line JSON object {node_id, long_name, short_name, hardware, port}
# to stdout, or returns non-zero with a message on stderr.
#
# node_id is derived from myNodeNum (stable, doesn't depend on the order
# the node database happens to print in).
parse_meshtastic_identity_from_info() {
    local info_file="$1" port="$2"

    python3 - "$info_file" "$port" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
port = sys.argv[2]
text = path.read_text(errors="replace")

m_num = re.search(r'"myNodeNum"\s*:\s*(\d+)', text)
if not m_num:
    raise SystemExit("myNodeNum not found")

node_num = int(m_num.group(1))
if not 0 <= node_num <= 0xFFFFFFFF:
    raise SystemExit(f"myNodeNum is outside uint32 range: {node_num}")

node_id = f"!{node_num:08x}"

m_owner = re.search(r'^Owner:\s*(.*?)\s+\(([^()]*)\)\s*$', text, re.MULTILINE)
if m_owner:
    long_name = m_owner.group(1).strip()
    short_name = m_owner.group(2).strip()
else:
    m_owner = re.search(r'^Owner:\s*(.*?)\s*$', text, re.MULTILINE)
    if not m_owner:
        raise SystemExit("Owner line not found")
    long_name = m_owner.group(1).strip()
    short_name = ""

m_hw = re.search(r'"hwModel"\s*:\s*"([^"]+)"', text)
hardware = (m_hw.group(1) if m_hw else "").strip()

if not long_name:
    long_name = node_id
if not short_name:
    short_name = node_id[-4:].upper()

print(json.dumps({
    "node_id": node_id,
    "long_name": long_name,
    "short_name": short_name,
    "hardware": hardware,
    "port": port,
}, ensure_ascii=False))
PY
}

# generate_config_from_radio <template> <output> <node_id> <long_name> \
#     <short_name> <hw_model> <radio_port> <chown_owner> <validate_python_bin> \
#     [validate_runner...]
#
# Writes config.py from config.example.py with the detected radio's values
# substituted in, then re-imports the generated file to confirm the exact
# values server.py would see match what was requested.
#
# <chown_owner> is passed straight to `chown` (e.g. "user:group") right
# after writing, before validation - pass "" to skip (install.sh already
# owns the files it writes; firstboot runs as root and needs to hand the
# file to the target user before that user's venv can read it back).
#
# <validate_python_bin> is the python interpreter used only for the final
# re-import check (the write step itself is plain stdlib and always uses
# `python3` directly - it doesn't need the project's venv). Passing the
# venv's python for validation is what makes that check mean something -
# checking config.py imports cleanly under a bare system python wouldn't
# prove anything about how server.py will actually load it.
#
# Any extra arguments after <validate_python_bin> are treated as a command
# prefix to run the validation python through (e.g. `runuser -u someuser
# --` on firstboot, where the install runs as root but the venv belongs to
# a different target user) - install.sh passes none, since it already runs
# as the owning user.
generate_config_from_radio() {
    local template="$1" output="$2" node_id="$3" long_name="$4"
    local short_name="$5" hw_model="$6" radio_port="$7" chown_owner="$8"
    local validate_python_bin="$9"
    shift 9
    local validate_runner=("$@")

    [[ -f "$template" ]] || { echo "Missing template: $template" >&2; return 1; }
    [[ ! -e "$output" ]] || { echo "$output already exists" >&2; return 1; }

    python3 - \
        "$template" \
        "$output" \
        "$node_id" \
        "$long_name" \
        "$short_name" \
        "$hw_model" \
        "$radio_port" <<'PY'
import ast
import re
import sys
from pathlib import Path

template = Path(sys.argv[1])
output = Path(sys.argv[2])
node_id, long_name, short_name, hw_model, radio_port = sys.argv[3:8]

text = template.read_text(encoding="utf-8")

def replace_simple_assignment(source: str, name: str, value) -> str:
    replacement = f"{name} = {value!r}"
    pattern = rf"(?m)^{re.escape(name)}\s*=.*$"
    new, count = re.subn(pattern, replacement, source, count=1)
    if count != 1:
        raise RuntimeError(f"Could not find assignment for {name}")
    return new

def replace_top_level_assignment_block(source: str, name: str, replacement: str) -> str:
    tree = ast.parse(source)
    target = None
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            names = []
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        names.append(t.id)
            elif isinstance(node.target, ast.Name):
                names.append(node.target.id)
            if name in names:
                target = node
                break
    if target is None or not hasattr(target, "end_lineno"):
        raise RuntimeError(f"Could not find top-level assignment for {name}")
    lines = source.splitlines()
    start = target.lineno - 1
    end = target.end_lineno
    lines[start:end] = replacement.splitlines()
    return "\n".join(lines) + ("\n" if source.endswith("\n") else "")

text = replace_simple_assignment(text, "MESHTASTIC_PORT", radio_port)
text = replace_simple_assignment(text, "LOCAL_NODE_ID", node_id)
text = replace_simple_assignment(text, "LOCAL_NODE_NAME", long_name)

known_nodes = (
    "KNOWN_NODES = {\n"
    f"    {node_id!r}: {long_name!r},\n"
    "}"
)
text = replace_top_level_assignment_block(text, "KNOWN_NODES", known_nodes)

known_node_info = (
    "KNOWN_NODE_INFO = {\n"
    f"    {node_id!r}: {{'short_name': {short_name!r}, 'hw_model': {hw_model!r}}},\n"
    "}"
)
text = replace_top_level_assignment_block(text, "KNOWN_NODE_INFO", known_node_info)

# Validate generated Python before writing.
ast.parse(text)

output.write_text(text, encoding="utf-8")
PY
    local write_status=$?
    if [[ $write_status -ne 0 ]]; then
        echo "Failed to generate $output from $template" >&2
        return 1
    fi

    if [[ -n "$chown_owner" ]]; then
        chown "$chown_owner" "$output"
        chmod 0644 "$output"
    fi

    # Validate the exact values that will be imported by server.py.
    "${validate_runner[@]}" "$validate_python_bin" - "$output" "$node_id" "$long_name" "$radio_port" <<'PY'
import importlib.util
import sys

path, expected_id, expected_name, expected_port = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("meshcenter_generated_config", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

checks = {
    "LOCAL_NODE_ID": (module.LOCAL_NODE_ID, expected_id),
    "LOCAL_NODE_NAME": (module.LOCAL_NODE_NAME, expected_name),
    "MESHTASTIC_PORT": (module.MESHTASTIC_PORT, expected_port),
}
bad = [f"{key}: {actual!r} != {expected!r}"
       for key, (actual, expected) in checks.items() if actual != expected]
if bad:
    raise SystemExit("; ".join(bad))
PY
    if [[ $? -ne 0 ]]; then
        echo "Generated $output failed validation" >&2
        rm -f "$output"
        return 1
    fi

    return 0
}
