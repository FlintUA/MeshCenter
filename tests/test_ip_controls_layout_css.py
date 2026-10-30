"""CAM2: the Pictures block of the network camera's Mode panel is a normal section under the stream facts (source-level)."""
import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "static" / "ui-kit.css").read_text(encoding="utf-8")


def _container_block(query):
    start = CSS.index(query)
    depth, i = 0, CSS.index("{", start)
    for j in range(i, len(CSS)):
        depth += CSS[j] == "{"
        depth -= CSS[j] == "}"
        if depth == 0:
            return CSS[i:j]
    raise AssertionError(query)


def test_wide_card_has_one_column_not_a_narrow_pictures_column():
    block = _container_block("@container (min-width: 640px)")
    assert "grid-template-columns: minmax(0, 1fr) minmax(220px, 300px)" not in block
    assert "#ipPanelMode {" not in block, "the panel is no two-column grid any more"
    assert "border-left" not in block and "flex-direction: column" not in block


def test_take_picture_and_quality_share_one_row():
    row = re.search(r"\.ip-snapshot-row\s*\{([^}]*)\}", CSS).group(1)
    assert "display: flex" in row and "flex-wrap: wrap" in row
    quality = re.search(r"\.ip-quality \.ip-select\s*\{([^}]*)\}", CSS).group(1)
    assert "min-height: 44px" in quality
    assert re.search(r"@container \(max-width: 480px\)\s*\{\s*\.ip-quality > span", CSS), "narrow card: the label is visually hidden, not the select"
