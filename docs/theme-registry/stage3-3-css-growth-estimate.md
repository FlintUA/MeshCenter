# Theme registry — Stage 3.3: CSS growth estimate & file-split decision

Report only. No `--mc-*` tokens, families, or CSS/JS/HTML files were touched in this stage.

## 1. Baseline measurements

**`static/ui-kit.css`**: 3605 lines, 113,190 bytes total.

Canonical token block (`:root` + `html[data-theme="dark"]`, under the "MESHCENTER CANONICAL DESIGN TOKENS" comment):

| Block | Lines | Bytes |
|---|---|---|
| `:root` (lines 3319–3478) | 160 | 5,681 |
| `html[data-theme="dark"]` (lines 3480–3604) | 125 | 3,852 |
| **Combined** | **285** | **9,533** (~8.4% of ui-kit.css) |

Leaf (literal value) vs. alias (`var(--mc-*)`, nothing else) declaration split within each block:

| Block | Leaf — color/shadow | Leaf — geometry/typography | Alias | Total decls |
|---|---|---|---|---|
| `:root` | 68 decls / 2,313 B | 19 decls / 619 B | 25 decls / 1,134 B | 112 |
| `html[data-theme="dark"]` | 81 decls / 2,765 B | 0 decls / 0 B | 12 decls / 499 B | 93 |

The geometry/typography tokens (~19, e.g. `--mc-body-size`, `--mc-card-radius`) live only in `:root` and are never redeclared in dark — confirmed by the 0-count above. Light has more aliases (25 vs. 12) and fewer color leaves (68 vs. 81) because several tokens that are aliases in light (`--mc-input-bg: var(--mc-bg-panel)`) become direct literal overrides in dark (`--mc-input-bg: #172535`) — an intentional asymmetry, not a measurement bug.

**All 5 theming CSS files combined** (`style-part1.css` + `style-part2.css` + `style-part3.css` + `style-part4.css` + `ui-kit.css`): 19,149 lines, 487,456 bytes.

| File | Lines | Bytes |
|---|---|---|
| style-part1.css | 3,518 | 73,481 |
| style-part2.css | 4,018 | 76,911 |
| style-part3.css | 3,871 | 94,806 |
| style-part4.css | 4,137 | 129,068 |
| ui-kit.css | 3,605 | 113,190 |

## 2. Alias resolution — confirmed, with one caveat

The brief's premise: a family override block only needs to redeclare *leaf* tokens; an alias like `--mc-surface: var(--mc-bg-panel)` doesn't need repeating, since it resolves through whatever `--mc-bg-panel` cascades to.

This is **correct for the real code shape**, verified empirically with a throwaway Playwright test (`.theme_stage0_scratch/`, deleted after use, not committed):

- First attempt used `:root` + a descendant selector (`[data-fam="x"]` on a child `<div>`). This **failed** — the alias resolved to the value at the element where it was *declared* (`:root`), not the value cascaded at the *inheriting* descendant element. This is a real, separate CSS gotcha (var() substitution happens once, at the declaring element, then the already-resolved value inherits down) — but it doesn't apply here.
- Second attempt mirrored the actual pattern: `:root` and `html[data-fam="x"]` both target the **same** `<html>` element — exactly like `:root` vs. the existing `html[data-theme="dark"]`, and exactly how a future `html[data-theme-family="sharp"]` block would be written. Result: the alias correctly resolved through the leaf-only override with zero redeclaration needed.

**Caveat for Stages 4–8**: this only holds as long as family override blocks use a same-element selector (`html[data-theme-family="..."]`, `:root[data-theme-family="..."]`), matching the existing dark-mode pattern. If a future family block were ever scoped to a descendant selector instead, aliases would need to be redeclared too. Given Stage 3.2 already established `data-theme-family` as an `<html>` attribute, this isn't expected to be an issue — just worth stating as the reasoning's boundary condition.

Conclusion: leaf-token bytes are the real marginal cost of one family's override block; alias bytes are not.

## 3. Per-family projection

Marginal cost = color/shadow leaf bytes only (excludes geometry/typography, which every family shares via `:root` and never redeclares).

- **Paired family** (light + dark leaf tokens) = 2,313 + 2,765 = 5,078 raw bytes. Add overhead margin for the wrapper selector line, closing brace, and a short header comment per block (~150 bytes × 2 blocks = 300 bytes, roughly matching the actual per-block overhead already present in the dark block: 3,852 total − 2,765 leaf − 499 alias = 588 bytes of selector/brace/whitespace for one block) → **~5,678 bytes/paired family**.
- **Fixed (single-mode) family** = roughly half: average of light/dark leaf bytes ≈ (2,313+2,765)/2 = 2,539, +~300 bytes overhead (one block) → **~2,839 bytes/fixed family**.

No in-repo plan document specifies which of Sharp/Gunmetal/Alpine/Teal Dark/Teal Light are paired vs. fixed (`grep -rl` across `docs/` for these names returns nothing beyond this stage's own brief). The brief's own naming is a signal, though: "Teal Dark" and "Teal Light" are named as two separate entries, unlike the single names "Sharp"/"Gunmetal"/"Alpine" — read here as Teal being two fixed families, the other three assumed paired (conservative default, since paired costs more).

| Scenario | Composition | Added bytes | ui-kit.css projected total | Growth |
|---|---|---|---|---|
| Worst case (all paired) | 5 × paired | 5 × 5,678 = 28,390 | 141,580 | +25.1% |
| Mixed (naming-based assumption) | 3 paired (Sharp/Gunmetal/Alpine) + 2 fixed (Teal Dark/Light) | 3×5,678 + 2×2,839 = 22,712 | 135,902 | +20.1% |
| All fixed (lower bound) | 5 × fixed | 5 × 2,839 = 14,195 | 127,385 | +12.5% |

## 4. Recommendation

None of the three scenarios move `ui-kit.css` past ~142 KB, a ~25% increase over today's 113 KB. For comparison, `style-part4.css` alone is already 129 KB and `style-part3.css` 95 KB — the codebase already routinely works with single CSS files in that size range without editor or diff friction being a reported problem. A concrete threshold: split only if a single file would clear roughly 250 KB (double the largest existing style file) or the token block itself would exceed ~30% of the file — neither is close under any scenario here (worst case the token block reaches ~38 KB of a 142 KB file, ~27%).

Splitting per-family files would also add real, ongoing cost that this stage's numbers don't offset: a new `<link>` tag per family in `templates/index.html`, a `?v=` cache-bust to keep in sync on every family edit, and cross-file lookups whenever auditing one token across families (today a single `grep` in `ui-kit.css` finds it).

**Recommendation: keep all family token blocks in `ui-kit.css`.** Do not split into per-family files for Stages 4–8. If a family roadmap well beyond the current five ever emerges (e.g. 15+), revisit with the same measurement approach — this projection only covers the five named families.

---
🤖 Generated with [Claude Code](https://claude.com/claude-code)
