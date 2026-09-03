#!/usr/bin/env python3
"""WCAG 2.2 relative luminance / contrast ratio checker for MeshCenter's
theme-registry work (Stage 0+). No third-party dependencies.

Usage:
    python contrast_check.py <hex1> <hex2> [<hex3> <hex4> ...]

Each pair of consecutive arguments is checked as a foreground/background pair.
Prints the WCAG 2.2 contrast ratio and whether it passes the standard text
thresholds (4.5:1 normal text / AA, 3:1 large text or UI components / AA,
7:1 / AAA).

Example:
    python contrast_check.py CC5500 263744
"""
import sys


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        raise ValueError(f"not a 6-digit (or 3-digit) hex color: {value!r}")
    r = int(value[0:2], 16)
    g = int(value[2:4], 16)
    b = int(value[4:6], 16)
    return r, g, b


def _channel_to_linear(c_8bit: int) -> float:
    # WCAG 2.x relative luminance formula (sRGB -> linear-light).
    c = c_8bit / 255.0
    if c <= 0.03928:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color: str) -> float:
    r, g, b = _hex_to_rgb(hex_color)
    r_lin = _channel_to_linear(r)
    g_lin = _channel_to_linear(g)
    b_lin = _channel_to_linear(b)
    # ITU-R BT.709 coefficients, per WCAG 2.x spec section 1.4.3 Appendix.
    return 0.2126 * r_lin + 0.7152 * g_lin + 0.0722 * b_lin


def contrast_ratio(hex_a: str, hex_b: str) -> float:
    l1 = relative_luminance(hex_a)
    l2 = relative_luminance(hex_b)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def describe(ratio: float) -> str:
    checks = []
    checks.append(("AA normal text (>=4.5:1)", ratio >= 4.5))
    checks.append(("AA large text / UI components (>=3:1)", ratio >= 3.0))
    checks.append(("AAA normal text (>=7:1)", ratio >= 7.0))
    return "\n".join(f"  [{'PASS' if ok else 'FAIL'}] {label}" for label, ok in checks)


def main(argv: list[str]) -> int:
    if len(argv) < 2 or len(argv) % 2 != 0:
        print(__doc__)
        return 1
    for i in range(0, len(argv), 2):
        a, b = argv[i], argv[i + 1]
        ratio = contrast_ratio(a, b)
        print(f"#{a.lstrip('#').upper()} vs #{b.lstrip('#').upper()}: {ratio:.2f}:1")
        print(describe(ratio))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
