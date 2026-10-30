"""CAM2: the network camera Mode panel is one compact row (Stream, Take picture, Picture quality) with one muted hint line (source-level)."""
import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "static" / "ui-kit.css").read_text(encoding="utf-8")


def test_mode_panel_is_one_compact_row_without_the_facts_grid():
    assert ".ip-info" not in CSS, "the facts grid styles are gone"
    row = re.search(r"\.ip-mode-row\s*\{([^}]*)\}", CSS).group(1)
    assert "display: flex" in row and "flex-wrap: wrap" in row
    field = re.search(r"\.ip-field \.ip-select\s*\{([^}]*)\}", CSS).group(1)
    assert "min-height: 44px" in field
    assert re.search(r"@container \(max-width: 480px\)\s*\{\s*\.ip-field > \.ip-field-label", CSS), "narrow card: labels visually hidden, selects keep their aria-label"


def test_hint_is_one_small_muted_line():
    hint = re.search(r"\.ip-hint\s*\{([^}]*)\}", CSS).group(1)
    assert "font-size: 0.85em" in hint and "color: var(--mc-text-secondary" in hint
