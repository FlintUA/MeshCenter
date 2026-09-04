# Raw-HEX color audit — Stage 0 baseline

_Generated 2026-09-03 for the theme-registry Stage 0 preparatory task._

Inventory of every unique 6-digit HEX literal across the 5 theming-relevant CSS files (`static/style-part1.css`–`style-part4.css`, `static/ui-kit.css`), classified for later token-normalization stages. **No values were changed and no components were touched** — this is a read-only inventory.

- Files scanned: `static/style-part1.css`, `static/style-part2.css`, `static/style-part3.css`, `static/style-part4.css`, `static/ui-kit.css` (18,707 lines total)
- Total HEX occurrences: **2019**
- Unique HEX values: **968**

## Classification method

Each occurrence was checked for (1) whether it sits inside a `/* ... */` comment rather than a live declaration, and (2) whether its selector/property context matches a semantic-status vocabulary (success/warning/danger/info, online/offline/medium, `.confirm-danger`, `notification-*`, `*-profile-badge`, etc.) versus general UI surface/text/border/shadow/gradient usage.

**Finding: no legitimate non-UI/vendor colors were found in these 5 files.** Before writing the classifier, two specific hypotheses for category (a) ("legitimate non-UI color") were checked directly against the codebase and both came back negative:

- **Chart/telemetry colors are not in CSS at all.** `chat-telemetry.js` sets Chart.js colors in JS (only 6 HEX literals there, out of scope for this CSS audit); the CSS side just themes the chart's *container* (`.telemetry-chart-container`), and the `<canvas>` itself is forced transparent (`style-part4.css:3265-3266`).
- **No per-node/avatar hash-color palette or Leaflet marker-color override exists in *our own* CSS.** `.base-node-avatar` / `.node-manager-avatar` rules are plain container chrome (border/background/hover), not a distinct-color-per-entity palette; the "EMBEDDED MAP WORKSPACE (Leaflet)" section (`style-part4.css:558`) only themes MeshCenter's own map toolbar/popup chrome, not Leaflet's own default marker icons.

So every live declaration found in the 5 files is a **product UI color** — either a general surface/text/border/gradient color, or (the one subtype worth distinguishing now) a **semantic status color** that later stages should map to a semantic token (e.g. `--mc-success`) rather than a plain surface token. A future stage that adds a real data-viz/vendor-override CSS section should re-run this audit rather than assume the same holds.

**Scope caveat — read before treating this as "no vendor colors exist anywhere":** this audit only grepped the 5 files listed above. Leaflet's own stylesheet (`https://unpkg.com/leaflet@1.9.4/dist/leaflet.css`, loaded via `<link>` straight from the CDN in `templates/index.html:15`) is a separate, external file and was **not** part of this grep at all — its absence from the registry below means "out of scope," not "checked and found clean." Leaflet's own default marker-icon colors (its classic blue pin, etc.) are real vendor colors; they just live outside the 5 files this Stage 0 task was scoped to. If a later stage wants to theme or override Leaflet's own chrome, that stylesheet needs its own separate look, not an assumption based on this document.

## Summary by category

| Category | Unique values | Description |
|---|---|---|
| `semantic-status-candidate` | 41 | Semantic status color (success/warning/danger/info) — normalization candidate, maps to a semantic token |
| `mixed:semantic-status-candidate+ui-normalization-candidate` | 16 | Mixed — same HEX reused as both a semantic-status color and a general surface color at different sites |
| `ui-normalization-candidate` | 911 | General product UI color (surface/text/border/shadow/gradient) — normalization candidate |
| `theme-family-token-value` | 14 | *(added Stage 4.1, not part of the Stage 0 baseline — see the addendum section at the end of this doc)* Canonical `--mc-*` token value for a non-Original theme family — this HEX **is** the token's declared value, not raw color leaking into component CSS |

## Full registry (grouped by HEX value)

Each row lists every `file:line` occurrence of that HEX value across the 5 files. Comment-only occurrences (design-review notes) are marked `[comment]` inline and were excluded from the category verdict.

### Semantic status color (success/warning/danger/info) — normalization candidate, maps to a semantic token

_41 unique values_

| HEX | Occurrences | Locations |
|---|---|---|
| `#16803C` | 2 | static/style-part4.css:1766, static/style-part4.css:1767 |
| `#173C2A` | 1 | static/ui-kit.css:3239 |
| `#217648` | 1 | static/style-part3.css:3723 |
| `#22A559` | 1 | static/style-part3.css:19 |
| `#23699F` | 1 | static/style-part3.css:3726 |
| `#277B4B` | 1 | static/style-part3.css:3600 |
| `#27B96F` | 1 | static/ui-kit.css:3108 |
| `#2A4A7A` | 1 | static/style-part1.css:1178 |
| `#2A6A3A` | 1 | static/style-part1.css:1176 |
| `#3A6A3A` | 1 | static/style-part1.css:1089 |
| `#433719` | 1 | static/ui-kit.css:3241 |
| `#49252B` | 1 | static/ui-kit.css:3243 |
| `#72D69A` | 2 | static/style-part3.css:3736, static/ui-kit.css:3238 |
| `#8FC7F0` | 1 | static/style-part3.css:3739 |
| `#986500` | 1 | static/style-part4.css:1768 |
| `#9B6B18` | 1 | static/style-part3.css:3601 |
| `#A93E46` | 1 | static/style-part3.css:3602 |
| `#B04A4A` | 1 | static/style-part1.css:1472 |
| `#B0D0B0` | 1 | static/style-part1.css:1089 |
| `#C78616` | 1 | static/style-part2.css:2893 |
| `#D0D8EC` | 1 | static/style-part1.css:1179 |
| `#D0E8D0` | 1 | static/style-part1.css:1177 |
| `#D74343` | 2 | static/style-part2.css:2839, static/style-part2.css:2895 |
| `#D94B57` | 1 | static/style-part4.css:885 |
| `#DB5A63` | 1 | static/style-part3.css:21 |
| `#E6505D` | 3 | static/ui-kit.css:60, static/ui-kit.css:61, static/ui-kit.css:3112 |
| `#E6AD22` | 1 | static/ui-kit.css:3110 |
| `#E6F1FA` | 1 | static/style-part3.css:3726 |
| `#E6F7EC` | 2 | static/style-part4.css:1766, static/style-part4.css:1767 |
| `#E7AF27` | 1 | static/style-part3.css:20 |
| `#E7F5EC` | 1 | static/style-part3.css:3723 |
| `#E8D8A0` | 1 | static/style-part1.css:1090 |
| `#E8ECF5` | 1 | static/style-part1.css:1178 |
| `#E8F0E8` | 1 | static/style-part1.css:1176 |
| `#E9F8F0` | 1 | static/ui-kit.css:3109 |
| `#ECD5D5` | 1 | static/style-part1.css:1473 |
| `#FCA5A5` | 1 | static/style-part4.css:1189 |
| `#FDECEE` | 2 | static/ui-kit.css:62, static/ui-kit.css:3113 |
| `#FF7B85` | 1 | static/ui-kit.css:3242 |
| `#FFF3D8` | 1 | static/style-part3.css:3724 |
| `#FFF4D8` | 1 | static/style-part4.css:1768 |

### Mixed — same HEX reused as both a semantic-status color and a general surface color at different sites

_16 unique values_

| HEX | Occurrences | Locations |
|---|---|---|
| `#239B62` | 2 | static/style-part2.css:2838, static/style-part2.css:2891 |
| `#7A6A2A` | 3 | static/style-part1.css:925, static/style-part1.css:1090, static/style-part1.css:1757 |
| `#8A3A2A` | 2 | static/style-part1.css:942, static/style-part1.css:1457 |
| `#946316` | 2 | static/style-part1.css:3013, static/style-part3.css:3724 |
| `#A12B2B` | 2 | static/style-part4.css:1168, static/style-part4.css:1175 |
| `#A43D45` | 3 | static/style-part3.css:317, static/style-part3.css:3725, static/style-part4.css:3906 |
| `#B83B47` | 2 | static/style-part4.css:1045, static/style-part4.css:1125 |
| `#D5D5D5` | 3 | static/style-part1.css:1091, static/style-part1.css:1456, static/style-part1.css:1475 |
| `#E8A89E` | 2 | static/style-part1.css:945, static/style-part1.css:1458 |
| `#E8E8E8` | 4 | static/style-part1.css:1409, static/style-part1.css:1455, static/style-part1.css:1474, static/style-part4.css:3914 |
| `#F0B8B0` | 2 | static/style-part1.css:941, static/style-part1.css:1457 |
| `#F0C86A` | 3 | static/style-part1.css:3018, static/style-part3.css:3737, static/ui-kit.css:3240 |
| `#F5E8E8` | 3 | static/style-part1.css:1472, static/style-part1.css:2514, static/style-part1.css:2582 |
| `#FDE9EB` | 2 | static/style-part3.css:3725, static/style-part4.css:3905 |
| `#FF9299` | 2 | static/style-part3.css:3738, static/style-part4.css:4017 |
| `#FFF7DF` | 2 | static/style-part2.css:1588, static/ui-kit.css:3111 |

### General product UI color (surface/text/border/shadow/gradient) — normalization candidate

_911 unique values_

| HEX | Occurrences | Locations |
|---|---|---|
| `#000000` | 1 | static/style-part4.css:2739 |
| `#0369A1` | 1 | static/style-part2.css:656 |
| `#080D13` | 1 | static/ui-kit.css:1869 |
| `#09111A` | 1 | static/style-part4.css:2060 |
| `#0A1119` | 1 | static/ui-kit.css:1764 |
| `#0A121C` | 3 | static/ui-kit.css:1801, static/ui-kit.css:2895, static/ui-kit.css:2904 |
| `#0B1520` | 1 | static/ui-kit.css:3219 |
| `#0D1723` | 1 | static/ui-kit.css:3200 |
| `#0E1B28` | 1 | static/style-part4.css:615 |
| `#0E1C2A` | 1 | static/style-part3.css:2555 |
| `#0F172A` | 4 | static/style-part2.css:423, static/style-part2.css:474, static/style-part2.css:532, static/style-part2.css:604 |
| `#0F1823` | 1 | static/ui-kit.css:1763 |
| `#0F1A26` | 3 | static/style-part4.css:2061, static/style-part4.css:2476, static/ui-kit.css:1677 |
| `#0F1C2A` | 1 | static/style-part4.css:3424 |
| `#0F1D2B` | 1 | static/ui-kit.css:2932 |
| `#101820` | 1 | static/style-part1.css:2166 |
| `#101923` | 1 | static/ui-kit.css:3205 |
| `#101B27` | 1 | static/style-part4.css:694 |
| `#101C29` | 1 | static/ui-kit.css:2431 |
| `#101D2B` | 1 | static/ui-kit.css:1972 |
| `#10233C` | 1 | static/style-part3.css:2232 |
| `#10233F` | 2 | static/style-part3.css:985, static/ui-kit.css:3086 |
| `#102F5C` | 1 | static/ui-kit.css:3129 |
| `#111821` | 1 | static/ui-kit.css:1050 |
| `#111A24` | 1 | static/ui-kit.css:1105 |
| `#111B27` | 1 | static/ui-kit.css:1864 |
| `#111C29` | 2 | static/ui-kit.css:3201, static/ui-kit.css:3275 |
| `#112F4D` | 1 | static/style-part3.css:2982 |
| `#121C28` | 1 | static/ui-kit.css:3255 |
| `#122F4A` | 1 | static/style-part3.css:3060 |
| `#1265D6` | 3 | static/style-part1.css:2290, static/style-part1.css:2305, static/style-part2.css:1063 |
| `#132334` | 3 | static/style-part4.css:3220, static/style-part4.css:3399, static/style-part4.css:4000 |
| `#142130` | 1 | static/ui-kit.css:1946 |
| `#142434` | 2 | static/style-part4.css:595, static/style-part4.css:851 |
| `#14243A` | 10 | static/style-part4.css:2038, static/style-part4.css:2347, static/style-part4.css:2358, static/style-part4.css:2372, static/style-part4.css:2380, static/style-part4.css:2410, static/style-part4.css:2753, static/style-part4.css:2832, static/style-part4.css:2884, static/style-part4.css:2933 |
| `#145FC6` | 3 | static/style-part2.css:823, static/style-part2.css:2736, static/style-part2.css:2810 |
| `#151E29` | 1 | static/ui-kit.css:1111 |
| `#15314F` | 1 | static/style-part3.css:2879 |
| `#1557B0` | 5 | static/style-part1.css:350, static/style-part1.css:423, static/style-part1.css:2330, static/style-part4.css:103, static/style-part4.css:263 |
| `#155FAE` | 1 | static/style-part4.css:3486 |
| `#162231` | 2 | static/ui-kit.css:3203, static/ui-kit.css:3264 |
| `#162331` | 2 | static/ui-kit.css:1731, static/ui-kit.css:2727 |
| `#162433` | 3 | static/style-part4.css:2062, static/style-part4.css:3334 [comment], static/ui-kit.css:3213 |
| `#16243A` | 1 | static/ui-kit.css:2685 |
| `#162534` | 1 | static/ui-kit.css:2659 |
| `#16283A` | 3 | static/style-part4.css:3421, static/style-part4.css:3428, static/style-part4.css:4009 |
| `#16324C` | 1 | static/style-part3.css:2520 |
| `#163E72` | 1 | static/style-part3.css:423 |
| `#165CBA` | 6 | static/style-part2.css:2936, static/style-part2.css:2946, static/style-part2.css:3368, static/style-part3.css:30, static/style-part3.css:311, static/ui-kit.css:2714 |
| `#166534` | 1 | static/style-part2.css:1624 |
| `#168447` | 1 | static/style-part4.css:1300 |
| `#168846` | 1 | static/ui-kit.css:1231 |
| `#16A34A` | 4 | static/style-part1.css:3468, static/style-part2.css:554, static/style-part2.css:1467, static/style-part2.css:1506 |
| `#172033` | 7 | static/style-part4.css:1263, static/style-part4.css:1329, static/style-part4.css:1358, static/style-part4.css:1398, static/style-part4.css:1493, static/style-part4.css:1671, static/style-part4.css:1937 |
| `#172432` | 2 | static/ui-kit.css:1685, static/ui-kit.css:2641 |
| `#17243A` | 8 | static/ui-kit.css:1220, static/ui-kit.css:1224, static/ui-kit.css:1226, static/ui-kit.css:1227, static/ui-kit.css:1228, static/ui-kit.css:1230, static/ui-kit.css:1231, static/ui-kit.css:1232 |
| `#172535` | 1 | static/ui-kit.css:3212 |
| `#172638` | 9 | static/style-part3.css:2353, static/style-part3.css:3734, static/style-part4.css:3251 [comment], static/style-part4.css:3254, static/style-part4.css:3269 [comment], static/style-part4.css:3972 [comment], static/style-part4.css:3978, static/style-part4.css:4025 [comment], static/style-part4.css:4031 |
| `#17283A` | 4 | static/style-part3.css:3460, static/style-part3.css:3551, static/style-part3.css:3732, static/style-part4.css:841 |
| `#17293A` | 2 | static/style-part4.css:577, static/style-part4.css:631 |
| `#172A3D` | 1 | static/style-part4.css:616 |
| `#17344F` | 3 | static/style-part3.css:2457, static/style-part3.css:2489, static/style-part3.css:2688 |
| `#17365F` | 1 | static/ui-kit.css:2589 |
| `#17392D` | 1 | static/ui-kit.css:2215 |
| `#173A5E` | 1 | static/style-part3.css:80 |
| `#173F73` | 1 | static/ui-kit.css:2507 |
| `#175FC0` | 1 | static/ui-kit.css:3101 |
| `#1769C2` | 3 | static/style-part3.css:2128, static/style-part4.css:3435, static/style-part4.css:3436 |
| `#1769D2` | 10 | static/style-part4.css:3109, static/ui-kit.css:1213, static/ui-kit.css:1214, static/ui-kit.css:1214, static/ui-kit.css:1226, static/ui-kit.css:1227, static/ui-kit.css:1229, static/ui-kit.css:1230, static/ui-kit.css:1230, static/ui-kit.css:1231 |
| `#1769E0` | 13 | static/style-part4.css:1392, static/style-part4.css:1393, static/style-part4.css:1713, static/style-part4.css:1803, static/style-part4.css:1804, static/style-part4.css:1821, static/style-part4.css:1833, static/style-part4.css:1874, static/style-part4.css:1875, static/style-part4.css:2041, static/style-part4.css:2414, static/style-part4.css:2415, static/style-part4.css:2540 |
| `#176B3A` | 1 | static/style-part4.css:1165 |
| `#18212C` | 8 | static/ui-kit.css:1066, static/ui-kit.css:1156, static/ui-kit.css:1175, static/ui-kit.css:1492, static/ui-kit.css:1497, static/ui-kit.css:2894, static/ui-kit.css:3005, static/ui-kit.css:3011 |
| `#182533` | 1 | static/ui-kit.css:2181 |
| `#182535` | 1 | static/ui-kit.css:3256 |
| `#182636` | 1 | static/ui-kit.css:3276 |
| `#183048` | 2 | static/style-part4.css:643, static/style-part4.css:644 |
| `#1A2535` | 1 | static/style-part4.css:3272 |
| `#1A2736` | 2 | static/ui-kit.css:2070, static/ui-kit.css:3294 |
| `#1A2A4A` | 24 | static/style-part1.css:36, static/style-part1.css:126, static/style-part1.css:176, static/style-part1.css:238, static/style-part1.css:357, static/style-part1.css:493, static/style-part1.css:1722, static/style-part1.css:1866, static/style-part1.css:2135, static/style-part1.css:2643, static/style-part2.css:3423, static/style-part2.css:3538, static/style-part2.css:3589, static/style-part2.css:3609, static/style-part2.css:3674, static/style-part2.css:3750, static/style-part2.css:3809, static/style-part2.css:3834, static/style-part2.css:3903, static/style-part2.css:3915, static/style-part2.css:3933, static/style-part2.css:3980, static/style-part3.css:353, static/style-part3.css:1905 |
| `#1A2C3F` | 1 | static/style-part3.css:3024 |
| `#1A3A2A` | 1 | static/style-part1.css:625 |
| `#1A3A5A` | 1 | static/style-part1.css:580 |
| `#1A5A7A` | 1 | static/style-part1.css:1743 |
| `#1A5FA8` | 1 | static/style-part3.css:3391 |
| `#1A73E8` | 24 | static/style-part1.css:350, static/style-part1.css:412, static/style-part1.css:423, static/style-part1.css:1956, static/style-part1.css:1958, static/style-part1.css:1988, static/style-part1.css:2249, static/style-part1.css:2272, static/style-part1.css:2280, static/style-part1.css:2325, static/style-part1.css:3291, static/style-part1.css:3292, static/style-part1.css:3496, static/style-part2.css:3543, static/style-part2.css:3544, static/style-part2.css:3597, static/style-part2.css:3787, static/style-part2.css:3789, static/style-part2.css:3846, static/style-part2.css:3848, static/style-part3.css:360, static/style-part3.css:363, static/style-part4.css:255, static/style-part4.css:302 |
| `#1A7F4B` | 1 | static/style-part2.css:1924 |
| `#1B2837` | 3 | static/ui-kit.css:1851, static/ui-kit.css:3202, static/ui-kit.css:3263 |
| `#1B2938` | 1 | static/ui-kit.css:2136 |
| `#1B2B3D` | 1 | static/ui-kit.css:3279 |
| `#1B2D40` | 5 | static/style-part4.css:3170, static/style-part4.css:3292, static/style-part4.css:3361, static/style-part4.css:3368, static/style-part4.css:3432 |
| `#1C2C3D` | 1 | static/style-part4.css:2063 |
| `#1C2E41` | 2 | static/style-part3.css:2533, static/style-part3.css:2771 |
| `#1D2B3A` | 1 | static/ui-kit.css:1879 |
| `#1D2B3B` | 1 | static/ui-kit.css:3257 |
| `#1D2D3D` | 1 | static/ui-kit.css:2646 |
| `#1D3043` | 1 | static/style-part4.css:3117 |
| `#1D344B` | 1 | static/ui-kit.css:1704 |
| `#1D3557` | 2 | static/style-part2.css:1771, static/style-part2.css:1821 |
| `#1D4ED8` | 1 | static/style-part2.css:437 |
| `#1D5DA8` | 1 | static/style-part2.css:909 |
| `#1E293B` | 5 | static/style-part2.css:452, static/style-part2.css:1532, static/style-part3.css:1500, static/style-part3.css:3640, static/ui-kit.css:2420 |
| `#1E3145` | 1 | static/style-part4.css:932 |
| `#1E3A8A` | 3 | static/style-part2.css:512, static/style-part2.css:584, static/style-part2.css:634 |
| `#1E3C72` | 8 | static/style-part1.css:256, static/style-part1.css:674, static/style-part1.css:675, static/style-part1.css:1152, static/style-part1.css:1189, static/style-part1.css:2096, static/style-part1.css:2097, static/style-part3.css:209 |
| `#1E4F8F` | 1 | static/style-part3.css:425 |
| `#1F2937` | 5 | static/style-part1.css:2844, static/style-part1.css:2880, static/style-part1.css:3116, static/style-part1.css:3160, static/style-part2.css:2786 |
| `#1F2D3D` | 1 | static/style-part4.css:163 |
| `#1F3347` | 1 | static/style-part4.css:3520 |
| `#1F6FD1` | 2 | static/style-part4.css:1048, static/style-part4.css:1048 |
| `#1F6FDB` | 4 | static/style-part3.css:1815, static/style-part3.css:1869, static/style-part4.css:837, static/ui-kit.css:3100 |
| `#1F6FE5` | 4 | static/style-part1.css:2956, static/style-part1.css:2958, static/style-part1.css:2997, static/style-part1.css:2999 |
| `#1F6FEB` | 1 | static/ui-kit.css:3097 |
| `#202936` | 1 | static/ui-kit.css:3298 |
| `#202A36` | 6 | static/ui-kit.css:1079, static/ui-kit.css:1144, static/ui-kit.css:1151, static/ui-kit.css:1504, static/ui-kit.css:1510, static/ui-kit.css:3004 |
| `#202E3E` | 1 | static/ui-kit.css:3294 |
| `#202F40` | 2 | static/ui-kit.css:2168, static/ui-kit.css:3204 |
| `#203044` | 1 | static/ui-kit.css:3277 |
| `#203247` | 1 | static/ui-kit.css:1965 |
| `#203449` | 2 | static/style-part3.css:3735, static/style-part4.css:3122 |
| `#20354A` | 1 | static/ui-kit.css:2734 |
| `#20364B` | 1 | static/style-part4.css:3126 |
| `#20384E` | 3 | static/style-part4.css:586, static/style-part4.css:852, static/style-part4.css:943 |
| `#203A52` | 1 | static/style-part4.css:617 |
| `#203B53` | 2 | static/style-part4.css:638, static/style-part4.css:889 |
| `#203D5D` | 2 | static/ui-kit.css:3232, static/ui-kit.css:3245 |
| `#205B9E` | 1 | static/ui-kit.css:1717 |
| `#21364B` | 1 | static/style-part3.css:3733 |
| `#21364D` | 1 | static/ui-kit.css:1510 |
| `#213B57` | 1 | static/ui-kit.css:1704 |
| `#214469` | 1 | static/style-part4.css:3510 |
| `#214D35` | 1 | static/style-part2.css:2348 |
| `#214F91` | 3 | static/ui-kit.css:48, static/ui-kit.css:3104, static/ui-kit.css:3130 |
| `#2168D7` | 2 | static/style-part4.css:2249, static/style-part4.css:2510 |
| `#216B36` | 1 | static/style-part2.css:2286 |
| `#216B3A` | 2 | static/style-part2.css:2513, static/style-part4.css:3341 [comment] |
| `#218653` | 2 | static/style-part3.css:1296, static/style-part4.css:949 |
| `#222D3A` | 1 | static/ui-kit.css:1119 |
| `#223245` | 1 | static/ui-kit.css:2190 |
| `#22324A` | 3 | static/style-part4.css:93, static/style-part4.css:152, static/style-part4.css:293 |
| `#223349` | 7 | static/ui-kit.css:1983, static/ui-kit.css:2202, static/ui-kit.css:2221, static/ui-kit.css:3248, static/ui-kit.css:3265, static/ui-kit.css:3270, static/ui-kit.css:3284 |
| `#22364A` | 1 | static/ui-kit.css:2626 |
| `#22364B` | 1 | static/style-part3.css:2932 |
| `#22A447` | 1 | static/style-part2.css:1381 |
| `#22C55E` | 4 | static/style-part1.css:1330, static/style-part1.css:3459, static/style-part2.css:1507, static/style-part4.css:3038 |
| `#233346` | 1 | static/ui-kit.css:1664 |
| `#23415F` | 1 | static/ui-kit.css:1712 |
| `#236337` | 2 | static/style-part2.css:1732, static/style-part2.css:1895 |
| `#23663A` | 1 | static/style-part2.css:2583 |
| `#243243` | 1 | static/ui-kit.css:3225 |
| `#243247` | 1 | static/style-part3.css:649 |
| `#243447` | 3 | static/style-part2.css:1802, static/style-part2.css:1871, static/style-part3.css:1599 |
| `#24374B` | 1 | static/style-part4.css:2064 |
| `#243A2B` | 1 | static/style-part2.css:1714 |
| `#243B5A` | 1 | static/style-part3.css:517 |
| `#243C54` | 1 | static/style-part4.css:3146 |
| `#24415F` | 2 | static/style-part1.css:3492, static/style-part2.css:163 |
| `#24425E` | 2 | static/style-part4.css:892, static/style-part4.css:1047 |
| `#24456F` | 2 | static/style-part3.css:230, static/style-part3.css:418 |
| `#24457B` | 2 | static/style-part3.css:1966, static/ui-kit.css:803 |
| `#244D82` | 1 | static/ui-kit.css:1007 |
| `#2470D8` | 3 | static/style-part2.css:2662, static/style-part2.css:2684, static/style-part2.css:2685 |
| `#25394D` | 1 | static/style-part4.css:2065 |
| `#255984` | 1 | static/style-part4.css:3918 |
| `#2563EB` | 1 | static/style-part2.css:1471 |
| `#25C66B` | 1 | static/ui-kit.css:1222 |
| `#263547` | 2 | static/ui-kit.css:1771, static/ui-kit.css:1865 |
| `#26364B` | 2 | static/ui-kit.css:839, static/ui-kit.css:3138 |
| `#26394D` | 1 | static/style-part3.css:3455 |
| `#263B50` | 4 | static/style-part4.css:3227, static/style-part4.css:3310 [comment], static/style-part4.css:3315, static/style-part4.css:3361 |
| `#263B52` | 6 | static/style-part3.css:958, static/style-part3.css:1100, static/style-part3.css:1201, static/style-part4.css:3769, static/style-part4.css:3803, static/style-part4.css:3889 |
| `#263C52` | 8 | static/style-part3.css:2352, static/style-part4.css:3251 [comment], static/style-part4.css:3255, static/style-part4.css:3273, static/style-part4.css:3972 [comment], static/style-part4.css:3977, static/style-part4.css:4025 [comment], static/style-part4.css:4030 |
| `#263D52` | 1 | static/style-part3.css:3024 |
| `#263E58` | 1 | static/ui-kit.css:1671 |
| `#263F5A` | 1 | static/ui-kit.css:1704 |
| `#267340` | 2 | static/style-part2.css:2453, static/style-part4.css:3342 [comment] |
| `#273140` | 1 | static/ui-kit.css:1934 |
| `#273D53` | 1 | static/ui-kit.css:2631 |
| `#27435C` | 1 | static/style-part4.css:641 |
| `#274766` | 3 | static/style-part3.css:665, static/ui-kit.css:395, static/ui-kit.css:3122 |
| `#275F8D` | 1 | static/style-part2.css:2589 |
| `#285071` | 2 | static/style-part4.css:639, static/style-part4.css:890 |
| `#28663A` | 1 | static/style-part2.css:1669 |
| `#286DA8` | 1 | static/style-part3.css:3603 |
| `#294766` | 1 | static/style-part3.css:2576 |
| `#29485D` | 1 | static/style-part2.css:2300 |
| `#294967` | 1 | static/ui-kit.css:3233 |
| `#2A3A6A` | 2 | static/style-part1.css:36, static/style-part3.css:1905 |
| `#2A3E53` | 1 | static/style-part4.css:2067 |
| `#2A3F54` | 4 | static/style-part4.css:3987, static/style-part4.css:4010, static/style-part4.css:4012, static/style-part4.css:4020 |
| `#2A4A6A` | 2 | static/style-part1.css:587, static/style-part3.css:103 |
| `#2A5684` | 1 | static/style-part4.css:3515 |
| `#2A5A3A` | 1 | static/style-part1.css:605 |
| `#2A5A8A` | 4 | static/style-part1.css:764, static/style-part1.css:1789, static/style-part1.css:2149, static/style-part1.css:2737 |
| `#2A9A60` | 1 | static/style-part4.css:2901 |
| `#2B3949` | 3 | static/ui-kit.css:1127, static/ui-kit.css:3006, static/ui-kit.css:3012 |
| `#2B3A4C` | 1 | static/ui-kit.css:1870 |
| `#2B3B50` | 1 | static/style-part3.css:701 |
| `#2B4059` | 5 | static/ui-kit.css:1990, static/ui-kit.css:2209, static/ui-kit.css:3249, static/ui-kit.css:3271, static/ui-kit.css:3285 |
| `#2B405B` | 1 | static/ui-kit.css:1181 |
| `#2B72DC` | 1 | static/style-part4.css:2941 |
| `#2C3E50` | 1 | static/style-part1.css:14 |
| `#2C6B52` | 1 | static/ui-kit.css:2216 |
| `#2CA56C` | 1 | static/style-part4.css:1136 |
| `#2D3D50` | 1 | static/ui-kit.css:2072 |
| `#2D4057` | 7 | static/style-part3.css:934, static/style-part3.css:966, static/style-part3.css:1040, static/style-part3.css:1167, static/style-part3.css:2113, static/style-part4.css:3721, static/style-part4.css:3946 |
| `#2D6CA3` | 2 | static/style-part4.css:3917, static/style-part4.css:3956 |
| `#2E4258` | 1 | static/ui-kit.css:3278 |
| `#2ECC71` | 1 | static/style-part1.css:1897 |
| `#2F3B4D` | 1 | static/style-part2.css:214 |
| `#2F3E50` | 2 | static/ui-kit.css:3224, static/ui-kit.css:3266 |
| `#2F4053` | 1 | static/ui-kit.css:2430 |
| `#2F4357` | 1 | static/ui-kit.css:2640 |
| `#2F73D9` | 1 | static/ui-kit.css:3096 |
| `#2F7DF6` | 1 | static/style-part2.css:1385 |
| `#2F80ED` | 1 | static/ui-kit.css:3114 |
| `#304A62` | 3 | static/style-part4.css:3234, static/style-part4.css:3310 [comment], static/style-part4.css:3320 |
| `#304A63` | 1 | static/style-part4.css:616 |
| `#31465C` | 1 | static/style-part3.css:2361 |
| `#31475F` | 1 | static/ui-kit.css:1856 |
| `#31485E` | 1 | static/style-part4.css:3212 |
| `#314A62` | 5 | static/style-part4.css:576, static/style-part4.css:594, static/style-part4.css:851, static/style-part4.css:856, static/style-part4.css:856 |
| `#315B3D` | 1 | static/style-part2.css:2280 |
| `#32445B` | 1 | static/ui-kit.css:2111 |
| `#33404F` | 1 | static/style-part2.css:2414 |
| `#334155` | 9 | static/style-part1.css:2943, static/style-part2.css:53, static/style-part2.css:1016, static/style-part2.css:1643, static/style-part2.css:2497, static/ui-kit.css:897, static/ui-kit.css:924, static/ui-kit.css:2398, static/ui-kit.css:2498 |
| `#334456` | 1 | static/ui-kit.css:1874 |
| `#33465C` | 1 | static/ui-kit.css:1785 |
| `#33475B` | 1 | static/style-part2.css:2958 |
| `#334A61` | 4 | static/style-part3.css:2532, static/style-part3.css:2554, static/style-part3.css:2770, static/style-part3.css:2778 |
| `#334B68` | 1 | static/ui-kit.css:3128 |
| `#344050` | 1 | static/ui-kit.css:1112 |
| `#344359` | 2 | static/style-part3.css:810, static/style-part3.css:870 |
| `#34445F` | 5 | static/style-part2.css:815, static/style-part2.css:892, static/style-part2.css:2729, static/style-part2.css:2791, static/style-part2.css:2803 |
| `#34475B` | 5 | static/ui-kit.css:2137, static/ui-kit.css:2162, static/ui-kit.css:2163, static/ui-kit.css:2164, static/ui-kit.css:2182 |
| `#34475D` | 1 | static/ui-kit.css:2931 |
| `#34485A` | 1 | static/style-part3.css:2783 |
| `#34485E` | 1 | static/style-part3.css:3734 |
| `#34495E` | 2 | static/style-part2.css:1784, static/style-part2.css:1856 |
| `#344A60` | 2 | static/style-part3.css:3550, static/style-part3.css:3732 |
| `#344A61` | 1 | static/style-part4.css:2066 |
| `#344A62` | 1 | static/style-part3.css:1379 |
| `#344B61` | 5 | static/style-part4.css:3161, static/style-part4.css:3177, static/style-part4.css:3373, static/style-part4.css:3429, static/style-part4.css:4019 |
| `#35475A` | 2 | static/ui-kit.css:1845, static/ui-kit.css:1852 |
| `#364253` | 8 | static/ui-kit.css:1080, static/ui-kit.css:1157, static/ui-kit.css:1167, static/ui-kit.css:1493, static/ui-kit.css:1505, static/ui-kit.css:1511, static/ui-kit.css:3008, static/ui-kit.css:3295 |
| `#36506A` | 1 | static/ui-kit.css:1954 |
| `#36516B` | 1 | static/style-part4.css:632 |
| `#36516C` | 1 | static/ui-kit.css:2625 |
| `#365A7A` | 1 | static/style-part3.css:2576 |
| `#374151` | 5 | static/style-part1.css:2931, static/style-part1.css:3149, static/style-part1.css:3248, static/style-part1.css:3287, static/style-part2.css:1954 |
| `#385068` | 4 | static/style-part4.css:3208, static/style-part4.css:3221, static/style-part4.css:3400, static/style-part4.css:4001 |
| `#385069` | 1 | static/style-part4.css:3118 |
| `#38516A` | 1 | static/style-part4.css:944 |
| `#386445` | 1 | static/style-part2.css:1276 |
| `#38D68A` | 2 | static/style-part4.css:612, static/style-part4.css:629 |
| `#3A4A5A` | 5 | static/style-part1.css:1029, static/style-part1.css:1051, static/style-part1.css:1415, static/style-part2.css:3480, static/style-part2.css:3634 |
| `#3A4A6A` | 1 | static/style-part1.css:794 |
| `#3A4E64` | 1 | static/ui-kit.css:2191 |
| `#3A5269` | 2 | static/ui-kit.css:2645, static/ui-kit.css:2658 |
| `#3A526A` | 4 | static/style-part4.css:3136, static/style-part4.css:3171, static/style-part4.css:3361, static/style-part4.css:3369 |
| `#3A526D` | 7 | static/ui-kit.css:1974, static/ui-kit.css:1985, static/ui-kit.css:2203, static/ui-kit.css:2222, static/ui-kit.css:3250, static/ui-kit.css:3267, static/ui-kit.css:3286 |
| `#3A5973` | 2 | static/style-part4.css:638, static/style-part4.css:889 |
| `#3A5A7A` | 2 | static/style-part1.css:86, static/style-part1.css:1642 |
| `#3B4858` | 1 | static/ui-kit.css:1936 |
| `#3B4859` | 3 | static/ui-kit.css:1106, static/ui-kit.css:1120, static/ui-kit.css:1176 |
| `#3B526A` | 1 | static/style-part4.css:3519 |
| `#3B5670` | 1 | static/style-part4.css:3127 |
| `#3B82F6` | 3 | static/style-part4.css:3043, static/style-part4.css:3049, static/ui-kit.css:2414 |
| `#3D242B` | 1 | static/style-part4.css:823 |
| `#3DDC84` | 1 | static/style-part1.css:3377 |
| `#3F5065` | 1 | static/style-part2.css:1226 |
| `#3F6FAE` | 1 | static/ui-kit.css:968 |
| `#405067` | 1 | static/ui-kit.css:2726 |
| `#405268` | 1 | static/style-part2.css:1337 |
| `#405369` | 1 | static/ui-kit.css:1896 |
| `#40556B` | 2 | static/style-part3.css:2366, static/style-part3.css:2931 |
| `#405875` | 1 | static/style-part2.css:1183 |
| `#41536A` | 1 | static/style-part3.css:1240 |
| `#41576D` | 1 | static/style-part2.css:2577 |
| `#415A72` | 5 | static/style-part4.css:3229, static/style-part4.css:3310 [comment], static/style-part4.css:3317, static/style-part4.css:3322, static/style-part4.css:4005 |
| `#415D78` | 1 | static/style-part3.css:126 |
| `#43566B` | 1 | static/ui-kit.css:2077 |
| `#43566D` | 1 | static/style-part3.css:837 |
| `#43A047` | 1 | static/style-part1.css:2764 |
| `#445164` | 3 | static/ui-kit.css:1145, static/ui-kit.css:1152, static/ui-kit.css:3007 |
| `#445366` | 3 | static/style-part4.css:3778, static/style-part4.css:3836, static/style-part4.css:3861 |
| `#45272B` | 1 | static/ui-kit.css:2227 |
| `#46617C` | 1 | static/ui-kit.css:1705 |
| `#475569` | 2 | static/style-part2.css:628, static/style-part2.css:1029 |
| `#48617E` | 1 | static/ui-kit.css:2614 |
| `#486B54` | 1 | static/style-part2.css:2002 |
| `#493B20` | 1 | static/ui-kit.css:2233 |
| `#496274` | 1 | static/style-part2.css:1988 |
| `#49689F` | 2 | static/style-part3.css:1978, static/style-part3.css:1979 |
| `#4A5A6A` | 4 | static/style-part1.css:877, static/style-part2.css:3472, static/style-part2.css:3688, static/style-part2.css:3945 |
| `#4A5A7A` | 2 | static/style-part1.css:173, static/style-part1.css:794 |
| `#4A7AAA` | 1 | static/style-part1.css:375 |
| `#4A9EFF` | 2 | static/style-part1.css:1881, static/style-part1.css:1896 |
| `#4AA3FF` | 2 | static/style-part4.css:613, static/style-part4.css:625 |
| `#4B5563` | 1 | static/style-part2.css:2820 |
| `#4B6685` | 1 | static/ui-kit.css:3251 |
| `#4C647B` | 1 | static/style-part3.css:3025 |
| `#4CAF50` | 4 | static/style-part1.css:644, static/style-part1.css:2364, static/style-part1.css:2754, static/style-part4.css:335 |
| `#4D6F9F` | 1 | static/ui-kit.css:1133 |
| `#4D7FB5` | 1 | static/style-part4.css:3509 |
| `#4EA1FF` | 1 | static/ui-kit.css:1966 |
| `#4F5F73` | 1 | static/style-part4.css:271 |
| `#4F8CFF` | 8 | static/style-part4.css:2042, static/style-part4.css:2391, static/style-part4.css:2392, static/style-part4.css:2409, static/style-part4.css:2793, static/style-part4.css:2840, static/style-part4.css:2897, static/style-part4.css:2940 |
| `#4F8FFF` | 2 | static/style-part4.css:2250, static/style-part4.css:2511 |
| `#4F9CFF` | 1 | static/ui-kit.css:3260 |
| `#4FD18B` | 1 | static/style-part4.css:957 |
| `#506983` | 6 | static/style-part4.css:2039, static/style-part4.css:2364, static/style-part4.css:2368, static/style-part4.css:2398, static/style-part4.css:2404, static/style-part4.css:2804 |
| `#51627B` | 1 | static/ui-kit.css:2113 |
| `#526174` | 1 | static/ui-kit.css:3299 |
| `#526177` | 2 | static/ui-kit.css:1221, static/ui-kit.css:1222 |
| `#526178` | 6 | static/style-part2.css:1056, static/style-part2.css:2486, static/style-part3.css:502, static/style-part3.css:1640, static/style-part3.css:1641, static/style-part3.css:3620 |
| `#52627A` | 2 | static/style-part3.css:1959, static/ui-kit.css:793 |
| `#52657A` | 1 | static/ui-kit.css:2280 |
| `#52657C` | 1 | static/ui-kit.css:1128 |
| `#53657B` | 1 | static/style-part2.css:2675 |
| `#536A83` | 1 | static/ui-kit.css:1900 |
| `#53A66E` | 1 | static/style-part2.css:2388 |
| `#55A9F5` | 1 | static/style-part4.css:586 |
| `#55B870` | 1 | static/style-part2.css:3283 |
| `#58677B` | 3 | static/ui-kit.css:1226, static/ui-kit.css:1230, static/ui-kit.css:1231 |
| `#58708F` | 2 | static/ui-kit.css:39, static/ui-kit.css:3087 |
| `#58B76C` | 4 | static/style-part2.css:3320, static/style-part2.css:3350, static/style-part4.css:3582 [comment], static/style-part4.css:3591 |
| `#596675` | 1 | static/style-part2.css:2357 |
| `#5A6A7A` | 1 | static/style-part2.css:3458 |
| `#5AA7FF` | 1 | static/ui-kit.css:2733 |
| `#5B82AA` | 4 | static/ui-kit.css:1991, static/ui-kit.css:2066, static/ui-kit.css:2101, static/ui-kit.css:2210 |
| `#5C6B7E` | 1 | static/ui-kit.css:998 |
| `#5C7082` | 1 | static/style-part4.css:640 |
| `#5D7286` | 1 | static/style-part4.css:977 |
| `#5D9B72` | 1 | static/style-part2.css:2522 |
| `#5DA4EF` | 2 | static/style-part3.css:2915, static/style-part3.css:2920 |
| `#5E7388` | 1 | static/style-part3.css:2415 |
| `#60748B` | 1 | static/style-part2.css:3249 |
| `#60758A` | 1 | static/style-part3.css:2496 |
| `#607A92` | 1 | static/style-part3.css:3042 |
| `#617184` | 1 | static/style-part2.css:2338 |
| `#617386` | 1 | static/style-part2.css:3106 |
| `#64748B` | 26 | static/style-part1.css:3452, static/style-part1.css:3474, static/style-part2.css:429, static/style-part2.css:470, static/style-part2.css:542, static/style-part2.css:571, static/style-part2.css:610, static/style-part2.css:1476, static/style-part2.css:1631, static/style-part3.css:406, static/style-part3.css:1988, static/style-part3.css:2402, static/style-part3.css:3185, static/style-part3.css:3527, static/style-part4.css:1215, static/style-part4.css:1251, static/style-part4.css:1277, static/style-part4.css:1349, static/style-part4.css:1538, static/style-part4.css:1677, static/style-part4.css:1688, static/style-part4.css:1748, static/style-part4.css:1761, static/style-part4.css:1797, static/style-part4.css:1815, static/ui-kit.css:2401 |
| `#64798D` | 1 | static/style-part3.css:2484 |
| `#647B91` | 1 | static/style-part3.css:2672 |
| `#654B18` | 1 | static/style-part1.css:3502 |
| `#65A3FF` | 1 | static/style-part4.css:2071 |
| `#667085` | 4 | static/style-part3.css:39, static/style-part3.css:281, static/style-part3.css:348, static/style-part4.css:439 |
| `#667487` | 1 | static/style-part3.css:721 |
| `#66756B` | 1 | static/style-part2.css:1918 |
| `#66758A` | 1 | static/style-part3.css:1357 |
| `#66788A` | 1 | static/style-part2.css:2989 |
| `#66788F` | 2 | static/style-part4.css:1045, static/style-part4.css:1096 |
| `#6688B7` | 2 | static/ui-kit.css:1134, static/ui-kit.css:1182 |
| `#66A8FF` | 2 | static/ui-kit.css:1672, static/ui-kit.css:3230 |
| `#66B2FF` | 1 | static/ui-kit.css:2929 |
| `#67788D` | 1 | static/ui-kit.css:3300 |
| `#687585` | 1 | static/style-part2.css:2319 |
| `#68758A` | 1 | static/ui-kit.css:1212 |
| `#68788B` | 1 | static/style-part4.css:3494 |
| `#68A77A` | 2 | static/style-part2.css:1743, static/style-part2.css:1904 |
| `#69A87F` | 1 | static/style-part2.css:2458 |
| `#6A5A3A` | 1 | static/style-part1.css:2341 |
| `#6A7A8A` | 9 | static/style-part1.css:1073, static/style-part2.css:3444, static/style-part2.css:3497, static/style-part2.css:3603, static/style-part2.css:3668, static/style-part2.css:3744, static/style-part2.css:3775, static/style-part2.css:3828, static/style-part2.css:3897 |
| `#6A97A6` | 1 | static/style-part2.css:3254 |
| `#6A9AB8` | 1 | static/style-part1.css:1739 |
| `#6AA7FF` | 1 | static/ui-kit.css:3226 |
| `#6B5200` | 1 | static/style-part3.css:255 |
| `#6B7280` | 11 | static/style-part1.css:2850, static/style-part1.css:2886, static/style-part1.css:3122, static/style-part1.css:3178, static/style-part1.css:3225, static/style-part2.css:143, static/style-part2.css:1968, static/style-part2.css:2780, static/style-part2.css:2840, static/style-part2.css:2841, static/style-part4.css:1169 |
| `#6B778C` | 1 | static/style-part4.css:147 |
| `#6B7A8D` | 1 | static/style-part3.css:1107 |
| `#6B8976` | 1 | static/style-part2.css:2372 |
| `#6C8195` | 1 | static/style-part3.css:2448 |
| `#6D7C8F` | 1 | static/style-part3.css:1300 |
| `#6E9FE8` | 2 | static/style-part3.css:1972, static/ui-kit.css:811 |
| `#6F7D73` | 1 | static/style-part2.css:1704 |
| `#6F7E90` | 1 | static/style-part3.css:1333 |
| `#6F94C6` | 1 | static/ui-kit.css:1005 |
| `#6FA67C` | 2 | static/style-part2.css:3067, static/style-part2.css:3068 |
| `#6FAC88` | 1 | static/style-part2.css:2313 |
| `#6FDC8C` | 1 | static/ui-kit.css:2125 |
| `#708399` | 1 | static/style-part4.css:882 |
| `#718095` | 1 | static/style-part3.css:1182 |
| `#718096` | 1 | static/ui-kit.css:889 |
| `#718198` | 4 | static/style-part2.css:2210, static/style-part2.css:2362, static/style-part3.css:131, static/ui-kit.css:2596 |
| `#718399` | 1 | static/ui-kit.css:2071 |
| `#71879B` | 1 | static/style-part3.css:2788 |
| `#72B8FF` | 1 | static/style-part4.css:619 |
| `#738096` | 6 | static/ui-kit.css:1226, static/ui-kit.css:1227, static/ui-kit.css:1228, static/ui-kit.css:1228, static/ui-kit.css:1230, static/ui-kit.css:1232 |
| `#738191` | 2 | static/style-part2.css:3018, static/style-part2.css:3084 |
| `#73ADFF` | 1 | static/style-part4.css:2072 |
| `#73B2FF` | 1 | static/ui-kit.css:3227 |
| `#744047` | 1 | static/ui-kit.css:2228 |
| `#748278` | 1 | static/style-part2.css:1675 |
| `#75869C` | 1 | static/style-part4.css:1041 |
| `#76602F` | 1 | static/ui-kit.css:2234 |
| `#77869A` | 1 | static/style-part4.css:3020 |
| `#788596` | 1 | static/style-part2.css:1328 |
| `#78899E` | 1 | static/style-part4.css:1048 |
| `#7890AA` | 1 | static/style-part3.css:3515 |
| `#78B6FF` | 1 | static/ui-kit.css:3244 |
| `#79879A` | 12 | static/style-part3.css:972, static/style-part3.css:995, static/style-part3.css:1094, static/style-part3.css:1125, static/style-part3.css:1135, static/style-part3.css:1142, static/style-part3.css:1174, static/style-part3.css:2122, static/style-part4.css:3727, static/style-part4.css:3755, static/style-part4.css:3878, static/style-part4.css:3896 |
| `#7A6A5A` | 1 | static/style-part1.css:364 |
| `#7A6B5A` | 1 | static/style-part1.css:739 |
| `#7A8391` | 1 | static/style-part2.css:865 |
| `#7A8595` | 1 | static/style-part4.css:172 |
| `#7A8794` | 3 | static/style-part2.css:1791, static/style-part2.css:1829, static/style-part2.css:1911 |
| `#7A8798` | 3 | static/style-part4.css:286, static/ui-kit.css:878, static/ui-kit.css:3139 |
| `#7A8AA0` | 5 | static/style-part2.css:3431, static/style-part2.css:3488, static/style-part2.css:3531, static/style-part2.css:3927, static/style-part3.css:3377 |
| `#7A8AAA` | 1 | static/style-part1.css:987 |
| `#7A8BA0` | 1 | static/style-part4.css:1048 |
| `#7A8CA5` | 1 | static/style-part4.css:992 |
| `#7B5D16` | 1 | static/style-part2.css:2595 |
| `#7B8492` | 1 | static/style-part2.css:949 |
| `#7B8796` | 7 | static/style-part2.css:220, static/style-part3.css:656, static/style-part3.css:706, static/style-part3.css:859, static/style-part3.css:3661, static/style-part3.css:3729, static/style-part3.css:3730 |
| `#7C641D` | 1 | static/style-part2.css:925 |
| `#7C8999` | 1 | static/style-part3.css:1209 |
| `#7D8FA1` | 1 | static/style-part3.css:2732 |
| `#7D91A7` | 2 | static/style-part4.css:2040, static/style-part4.css:2385 |
| `#7E2530` | 1 | static/style-part4.css:808 |
| `#7EB28D` | 1 | static/style-part2.css:1880 |
| `#7EB391` | 1 | static/style-part2.css:2510 |
| `#7F8D9D` | 1 | static/style-part4.css:3603 |
| `#7F8FA2` | 2 | static/ui-kit.css:527, static/ui-kit.css:611 |
| `#7FB0DC` | 1 | static/style-part4.css:4022 |
| `#806A25` | 1 | static/style-part2.css:1270 |
| `#813842` | 1 | static/style-part4.css:3239 |
| `#8293A5` | 2 | static/style-part3.css:2423, static/style-part3.css:2503 |
| `#8297AB` | 1 | static/ui-kit.css:1682 |
| `#829BB5` | 1 | static/ui-kit.css:3169 |
| `#8390A0` | 1 | static/style-part3.css:1234 |
| `#8492A6` | 1 | static/style-part4.css:1042 |
| `#8592A3` | 3 | static/style-part2.css:2710, static/style-part2.css:2750, static/style-part2.css:2771 |
| `#86EFAC` | 1 | static/style-part4.css:1181 |
| `#8793A2` | 1 | static/style-part3.css:1249 |
| `#8794A4` | 1 | static/style-part3.css:1366 |
| `#8795A8` | 1 | static/ui-kit.css:2532 |
| `#8798AD` | 1 | static/ui-kit.css:3088 |
| `#87BCE7` | 1 | static/ui-kit.css:265 |
| `#8995A2` | 1 | static/style-part2.css:3043 |
| `#8995A5` | 1 | static/ui-kit.css:911 |
| `#8A3A3A` | 1 | static/style-part1.css:2515 |
| `#8A5A00` | 1 | static/style-part2.css:1589 |
| `#8A6A5A` | 1 | static/style-part1.css:1121 |
| `#8A7A4A` | 1 | static/style-part1.css:915 |
| `#8A94A3` | 2 | static/style-part2.css:153, static/style-part3.css:785 |
| `#8A95A4` | 1 | static/style-part3.css:817 |
| `#8A96A5` | 3 | static/style-part3.css:1388, static/style-part3.css:1397, static/style-part3.css:2130 |
| `#8A96A6` | 1 | static/ui-kit.css:933 |
| `#8A9AA8` | 5 | static/style-part2.css:3581, static/style-part2.css:3615, static/style-part2.css:3694, static/style-part2.css:3781, static/style-part2.css:3838 |
| `#8A9BB5` | 2 | static/style-part1.css:728, static/style-part1.css:1781 |
| `#8AB4D8` | 1 | static/style-part1.css:1733 |
| `#8ABEFF` | 1 | static/ui-kit.css:3231 |
| `#8ABF99` | 2 | static/style-part2.css:1729, static/style-part2.css:1892 |
| `#8AC39A` | 1 | static/style-part2.css:2581 |
| `#8B3D34` | 1 | static/style-part2.css:2601 |
| `#8B94A3` | 1 | static/style-part2.css:854 |
| `#8BA0B7` | 1 | static/ui-kit.css:2538 |
| `#8BB7EB` | 1 | static/style-part4.css:3484 |
| `#8BC0FF` | 3 | static/style-part4.css:3984, static/ui-kit.css:1723, static/ui-kit.css:1726 |
| `#8BC34A` | 1 | static/style-part1.css:644 |
| `#8CE2B5` | 1 | static/ui-kit.css:2217 |
| `#8D4650` | 1 | static/style-part4.css:3526 |
| `#8EA6BC` | 1 | static/style-part3.css:2540 |
| `#8EB8DD` | 1 | static/style-part2.css:2587 |
| `#8F1D14` | 1 | static/style-part4.css:113 |
| `#8FA3B8` | 1 | static/ui-kit.css:1960 |
| `#8FA7C2` | 1 | static/ui-kit.css:2928 |
| `#8FB8FF` | 1 | static/style-part1.css:3397 |
| `#8FC4FF` | 1 | static/style-part3.css:3456 |
| `#8FC5FF` | 1 | static/ui-kit.css:1930 |
| `#90A5BA` | 1 | static/ui-kit.css:3218 |
| `#90A6BC` | 1 | static/ui-kit.css:2636 |
| `#90B080` | 2 | static/style-part1.css:1013, static/style-part1.css:1138 |
| `#914343` | 1 | static/style-part3.css:729 |
| `#91A2B6` | 3 | static/ui-kit.css:1140, static/ui-kit.css:1163, static/ui-kit.css:3010 |
| `#91A4B8` | 1 | static/ui-kit.css:2172 |
| `#91A6BC` | 1 | static/ui-kit.css:1838 |
| `#91A7BC` | 1 | static/style-part4.css:2070 |
| `#91A8BC` | 1 | static/style-part4.css:636 |
| `#927FB2` | 1 | static/style-part4.css:3640 |
| `#93463C` | 1 | static/style-part2.css:1282 |
| `#93A2B3` | 6 | static/style-part4.css:3693, static/style-part4.css:3779, static/style-part4.css:3794, static/style-part4.css:3810, static/style-part4.css:3817, static/style-part4.css:3927 |
| `#94A3B8` | 5 | static/style-part2.css:1499, static/style-part2.css:1525, static/style-part2.css:1541, static/style-part2.css:1573, static/style-part2.css:1600 |
| `#94A8BD` | 1 | static/ui-kit.css:2432 |
| `#94C7A5` | 1 | static/style-part2.css:2451 |
| `#96A6B8` | 1 | static/ui-kit.css:1098 |
| `#99434E` | 1 | static/style-part4.css:3246 |
| `#9A3434` | 2 | static/style-part2.css:931, static/style-part3.css:1121 |
| `#9A5A4A` | 1 | static/style-part1.css:932 |
| `#9A5B00` | 1 | static/style-part4.css:1166 |
| `#9AA0A6` | 1 | static/style-part4.css:331 |
| `#9AA4B2` | 2 | static/style-part3.css:17, static/style-part3.css:22 |
| `#9AA4B7` | 1 | static/style-part1.css:3393 |
| `#9AA5B4` | 5 | static/style-part2.css:2887, static/style-part3.css:3218, static/style-part3.css:3320, static/style-part3.css:3326, static/style-part3.css:3819 |
| `#9AA5B5` | 1 | static/style-part1.css:1058 |
| `#9AA7B5` | 1 | static/style-part2.css:3261 |
| `#9AA8B8` | 3 | static/ui-kit.css:477, static/ui-kit.css:522, static/ui-kit.css:606 |
| `#9AA8BA` | 2 | static/style-part3.css:439, static/style-part3.css:1117 |
| `#9B7421` | 2 | static/style-part3.css:1312, static/style-part4.css:953 |
| `#9B78C4` | 1 | static/style-part4.css:1137 |
| `#9BC7FF` | 1 | static/style-part4.css:3128 |
| `#9DB0C1` | 1 | static/style-part4.css:596 |
| `#9E7BC7` | 2 | static/style-part4.css:1996, static/style-part4.css:1998 |
| `#9EB1C6` | 2 | static/ui-kit.css:2034, static/ui-kit.css:2083 |
| `#9EB3C8` | 16 | static/style-part4.css:3198, static/style-part4.css:3252 [comment], static/style-part4.css:3259, static/style-part4.css:3325, static/style-part4.css:3380, static/style-part4.css:3404, static/style-part4.css:3407, static/style-part4.css:3974 [comment], static/style-part4.css:3983, static/style-part4.css:3985, static/style-part4.css:3996, static/style-part4.css:4023, static/style-part4.css:4027 [comment], static/style-part4.css:4036, static/style-part4.css:4038, static/style-part4.css:4039 |
| `#9EB5C9` | 1 | static/style-part3.css:3068 |
| `#9ECBB0` | 1 | static/style-part2.css:2301 |
| `#9FB0C2` | 2 | static/ui-kit.css:2157, static/ui-kit.css:2475 |
| `#9FB0C3` | 3 | static/ui-kit.css:2276, static/ui-kit.css:3269, static/ui-kit.css:3281 |
| `#9FB0C5` | 1 | static/style-part2.css:1198 |
| `#9FB1C4` | 1 | static/ui-kit.css:1887 |
| `#9FB1C5` | 1 | static/ui-kit.css:2654 |
| `#9FB3C8` | 1 | static/ui-kit.css:3168 |
| `#9FB4CC` | 1 | static/ui-kit.css:1978 |
| `#A16207` | 1 | static/style-part4.css:1305 |
| `#A23E48` | 1 | static/ui-kit.css:322 |
| `#A32929` | 2 | static/style-part2.css:2433, static/style-part4.css:3341 [comment] |
| `#A33F3F` | 1 | static/style-part3.css:848 |
| `#A5AFBA` | 3 | static/style-part2.css:3007, static/style-part2.css:3013, static/style-part2.css:3240 |
| `#A784CE` | 1 | static/style-part4.css:1146 |
| `#A8B0BA` | 1 | static/style-part2.css:1242 |
| `#A985D3` | 1 | static/style-part4.css:2297 |
| `#A9BAC9` | 1 | static/style-part4.css:618 |
| `#A9BED1` | 2 | static/style-part4.css:3185, static/style-part4.css:3383 |
| `#A9CBB3` | 1 | static/style-part2.css:1274 |
| `#AAB5C0` | 1 | static/style-part2.css:3699 |
| `#AD5360` | 1 | static/style-part4.css:3241 |
| `#AEB7C1` | 3 | static/style-part2.css:3297, static/style-part2.css:3362, static/style-part4.css:3585 [comment] |
| `#AEB9C8` | 1 | static/style-part2.css:1036 |
| `#AEBDCD` | 1 | static/ui-kit.css:1177 |
| `#AEBDCE` | 1 | static/style-part3.css:1653 |
| `#AEBED0` | 1 | static/ui-kit.css:2464 |
| `#AFC2D5` | 1 | static/style-part4.css:3521 |
| `#B0A090` | 1 | static/style-part1.css:1376 |
| `#B0B0B0` | 2 | static/style-part1.css:969, static/style-part1.css:1271 |
| `#B0B8C5` | 3 | static/style-part1.css:984, static/style-part1.css:1952, static/style-part2.css:3639 |
| `#B0C8A0` | 2 | static/style-part1.css:1000, static/style-part1.css:1126 |
| `#B0C8F0` | 1 | static/style-part1.css:1987 |
| `#B14B4B` | 1 | static/style-part3.css:1306 |
| `#B17A00` | 1 | static/style-part3.css:3538 |
| `#B42318` | 3 | static/style-part4.css:107, static/style-part4.css:1310, static/style-part4.css:3503 |
| `#B43842` | 1 | static/style-part3.css:3544 |
| `#B79AD9` | 3 | static/style-part4.css:1129, static/style-part4.css:1150, static/style-part4.css:1997 |
| `#B7C1D6` | 1 | static/style-part1.css:3342 |
| `#B7C6D6` | 2 | static/ui-kit.css:2186, static/ui-kit.css:3258 |
| `#B7D8C0` | 1 | static/style-part2.css:1658 |
| `#B82736` | 1 | static/style-part4.css:978 |
| `#B8C6D6` | 1 | static/ui-kit.css:1121 |
| `#B8C9D8` | 1 | static/style-part4.css:976 |
| `#B8CBE0` | 1 | static/ui-kit.css:2660 |
| `#B8CCE2` | 1 | static/ui-kit.css:403 |
| `#B91C1C` | 1 | static/style-part2.css:664 |
| `#B97870` | 2 | static/style-part2.css:3077, static/style-part2.css:3078 |
| `#B9C6D6` | 1 | static/style-part2.css:1180 |
| `#B9C7D6` | 1 | static/style-part2.css:2575 |
| `#B9C7DB` | 2 | static/style-part3.css:1967, static/ui-kit.css:804 |
| `#B9C8D8` | 2 | static/ui-kit.css:259, static/ui-kit.css:308 |
| `#B9C9DA` | 2 | static/style-part3.css:1290, static/ui-kit.css:2223 |
| `#B9DFC9` | 1 | static/style-part3.css:1294 |
| `#BDCADA` | 1 | static/ui-kit.css:1831 |
| `#BDCCDC` | 1 | static/ui-kit.css:3121 |
| `#BDD0E4` | 1 | static/style-part3.css:3732 |
| `#BFD0DF` | 1 | static/style-part4.css:892 |
| `#BFD0E8` | 1 | static/style-part2.css:906 |
| `#C0392B` | 2 | static/style-part2.css:1928, static/style-part3.css:1058 |
| `#C08880` | 1 | static/style-part1.css:1381 |
| `#C0A040` | 2 | static/style-part1.css:1382, static/style-part1.css:1753 |
| `#C0A84A` | 1 | static/style-part1.css:997 |
| `#C0C8D0` | 1 | static/style-part2.css:3468 |
| `#C0D4E8` | 4 | static/style-part1.css:767, static/style-part1.css:1799, static/style-part1.css:2156, static/style-part1.css:2746 |
| `#C2CEDA` | 1 | static/style-part3.css:2325 |
| `#C3D1DF` | 2 | static/style-part4.css:3336 [comment], static/ui-kit.css:3217 |
| `#C3D3E2` | 1 | static/style-part4.css:3418 |
| `#C3D3E3` | 1 | static/style-part4.css:2069 |
| `#C4D3DF` | 1 | static/style-part4.css:641 |
| `#C5D2DF` | 1 | static/ui-kit.css:1892 |
| `#C62828` | 4 | static/style-part1.css:2583, static/style-part2.css:3513, static/style-part2.css:3988, static/style-part4.css:183 |
| `#C6D7E8` | 1 | static/style-part4.css:3193 |
| `#C7A84D` | 2 | static/style-part2.css:3072, static/style-part2.css:3073 |
| `#C7CED6` | 1 | static/style-part2.css:3060 |
| `#C7D0DC` | 1 | static/ui-kit.css:950 |
| `#C7D4E1` | 1 | static/style-part3.css:2899 |
| `#C87D7D` | 1 | static/style-part2.css:2447 |
| `#C8D3DE` | 1 | static/style-part4.css:929 |
| `#C8D3DF` | 1 | static/style-part4.css:1048 |
| `#C8D5E5` | 1 | static/ui-kit.css:2576 |
| `#C9D5E1` | 1 | static/style-part3.css:2517 |
| `#C9D7E6` | 1 | static/ui-kit.css:2471 |
| `#C9D8EA` | 1 | static/ui-kit.css:2505 |
| `#C9E3FF` | 1 | static/style-part4.css:3511 |
| `#CAD4E0` | 3 | static/style-part2.css:1799, static/style-part2.css:1868, static/style-part3.css:1596 |
| `#CAD5E2` | 1 | static/style-part4.css:1048 |
| `#CBD4DF` | 1 | static/style-part2.css:1213 |
| `#CBD5DF` | 1 | static/style-part3.css:61 |
| `#CBD5E1` | 8 | static/style-part2.css:617, static/style-part2.css:632, static/style-part2.css:1026, static/style-part2.css:1565, static/style-part3.css:2229, static/style-part4.css:3766, static/style-part4.css:3800, static/style-part4.css:3833 |
| `#CBD6E1` | 1 | static/style-part3.css:2643 |
| `#CBD7E4` | 2 | static/style-part3.css:758, static/ui-kit.css:1113 |
| `#CBD7E6` | 1 | static/style-part4.css:1011 |
| `#CBD7E7` | 1 | static/style-part3.css:231 |
| `#CBD8CF` | 1 | static/style-part2.css:2265 |
| `#CBD8E6` | 5 | static/style-part3.css:662, static/style-part4.css:2881, static/style-part4.css:2930, static/ui-kit.css:2040, static/ui-kit.css:3120 |
| `#CBDFF7` | 1 | static/style-part2.css:918 |
| `#CCD3DC` | 1 | static/style-part2.css:2320 |
| `#CCD5DF` | 1 | static/style-part2.css:1261 |
| `#CCD6E2` | 1 | static/style-part3.css:2250 |
| `#CFD8E5` | 1 | static/style-part3.css:514 |
| `#CFDAE8` | 1 | static/ui-kit.css:2611 |
| `#D0D0D0` | 3 | static/style-part1.css:964, static/style-part1.css:1266, static/style-part1.css:1357 |
| `#D2DEEA` | 1 | static/style-part3.css:1179 |
| `#D34F5D` | 1 | static/style-part4.css:2905 |
| `#D3D3D3` | 8 | static/style-part4.css:3659, static/style-part4.css:3660, static/style-part4.css:3661, static/style-part4.css:3662, static/style-part4.css:3668, static/style-part4.css:3669, static/style-part4.css:3670, static/style-part4.css:3671 |
| `#D3DCE8` | 1 | static/style-part3.css:424 |
| `#D3EBDA` | 2 | static/style-part2.css:1744, static/style-part2.css:1905 |
| `#D4A31F` | 1 | static/style-part3.css:3558 |
| `#D4B85A` | 2 | static/style-part1.css:926, static/style-part1.css:1747 |
| `#D4C06A` | 1 | static/style-part1.css:993 |
| `#D4DBE5` | 2 | static/style-part3.css:201, static/style-part3.css:355 |
| `#D4DDE8` | 1 | static/style-part3.css:2285 |
| `#D4E0F0` | 1 | static/style-part1.css:227 |
| `#D5A4A4` | 1 | static/style-part2.css:2430 |
| `#D5C8A8` | 1 | static/style-part1.css:916 |
| `#D5CBB8` | 1 | static/style-part1.css:746 |
| `#D5D0C0` | 1 | static/style-part1.css:2345 |
| `#D5D0C8` | 3 | static/style-part1.css:779, static/style-part1.css:1195, static/style-part1.css:1667 |
| `#D5D8DD` | 20 | static/style-part1.css:81, static/style-part1.css:402, static/style-part1.css:447, static/style-part1.css:573, static/style-part1.css:598, static/style-part1.css:631, static/style-part1.css:637, static/style-part1.css:720, static/style-part1.css:1631, static/style-part1.css:1772, static/style-part1.css:1940, static/style-part1.css:1975, static/style-part1.css:2239, static/style-part1.css:2262, static/style-part2.css:2256, static/style-part2.css:3627, static/style-part2.css:3711, static/style-part2.css:3761, static/style-part2.css:3861, static/style-part2.css:3958 |
| `#D5DBE3` | 1 | static/style-part2.css:2411 |
| `#D5DCE6` | 1 | static/style-part2.css:2673 |
| `#D5DCE7` | 2 | static/style-part3.css:1956, static/ui-kit.css:790 |
| `#D5DDE5` | 1 | static/style-part2.css:1909 |
| `#D5DEE8` | 1 | static/style-part3.css:2440 |
| `#D5DFEB` | 6 | static/style-part4.css:2036, static/style-part4.css:2346, static/style-part4.css:2379, static/style-part4.css:2403, static/style-part4.css:2752, static/style-part4.css:2829 |
| `#D5E0EC` | 2 | static/style-part1.css:571, static/style-part3.css:60 |
| `#D5E2EF` | 1 | static/ui-kit.css:1713 |
| `#D5E9FC` | 1 | static/style-part4.css:3499 |
| `#D6E0EB` | 1 | static/style-part4.css:1117 |
| `#D74755` | 1 | static/style-part4.css:994 |
| `#D7DCE5` | 6 | static/style-part1.css:2941, static/style-part1.css:3185, static/style-part1.css:3219, static/style-part1.css:3232, static/style-part1.css:3260, static/style-part1.css:3285 |
| `#D7DEE8` | 5 | static/style-part3.css:402, static/ui-kit.css:836, static/ui-kit.css:851, static/ui-kit.css:852, static/ui-kit.css:3137 |
| `#D7E0E9` | 2 | static/style-part4.css:3008, static/style-part4.css:3492 |
| `#D7E0EA` | 4 | static/ui-kit.css:36, static/ui-kit.css:2682, static/ui-kit.css:3094, static/ui-kit.css:3164 |
| `#D7E2EC` | 1 | static/ui-kit.css:1844 |
| `#D7E4F1` | 1 | static/ui-kit.css:2093 |
| `#D8A51D` | 1 | static/style-part3.css:3540 |
| `#D8C788` | 1 | static/style-part2.css:1268 |
| `#D8DEE6` | 7 | static/style-part3.css:42, static/style-part3.css:294, static/style-part3.css:3425, static/style-part3.css:3437, static/style-part3.css:3515, static/style-part3.css:3617, static/style-part3.css:3637 |
| `#D8DEE7` | 2 | static/style-part3.css:208, static/style-part3.css:362 |
| `#D8DEE8` | 2 | static/style-part2.css:1007, static/style-part2.css:1103 |
| `#D8E0E8` | 3 | static/style-part4.css:840, static/style-part4.css:846, static/style-part4.css:3106 |
| `#D8E0EA` | 2 | static/style-part4.css:431, static/style-part4.css:999 |
| `#D8E1EA` | 2 | static/style-part3.css:2663, static/style-part3.css:2667 |
| `#D8E1EC` | 11 | static/style-part4.css:1221, static/style-part4.css:1381, static/style-part4.css:1459, static/style-part4.css:1595, static/style-part4.css:1610, static/style-part4.css:1656, static/style-part4.css:1705, static/style-part4.css:1713, static/style-part4.css:1794, static/style-part4.css:1874, static/ui-kit.css:2556 |
| `#D8E2ED` | 1 | static/ui-kit.css:1935 |
| `#D8E4F0` | 5 | static/style-part1.css:763, static/style-part1.css:1788, static/style-part1.css:2148, static/style-part1.css:2736, static/style-part2.css:172 |
| `#D9545F` | 2 | static/style-part3.css:3546, static/style-part3.css:3564 |
| `#D9606B` | 2 | static/style-part4.css:805, static/style-part4.css:818 |
| `#D9988A` | 1 | static/style-part1.css:943 |
| `#D99B91` | 1 | static/style-part2.css:2599 |
| `#D9B437` | 1 | static/style-part2.css:3324 |
| `#D9BD72` | 1 | static/style-part2.css:2593 |
| `#D9E0E7` | 1 | static/style-part3.css:2718 |
| `#D9E1EC` | 8 | static/ui-kit.css:1211, static/ui-kit.css:1218, static/ui-kit.css:1222, static/ui-kit.css:1224, static/ui-kit.css:1228, static/ui-kit.css:1230, static/ui-kit.css:1231, static/ui-kit.css:1232 |
| `#D9E3EF` | 1 | static/style-part4.css:248 |
| `#D9E7F5` | 5 | static/style-part4.css:3141, static/style-part4.css:3172, static/style-part4.css:3361, static/style-part4.css:3392, static/style-part4.css:3414 |
| `#DBE2EB` | 1 | static/ui-kit.css:995 |
| `#DBE3EE` | 2 | static/style-part2.css:510, static/style-part2.css:582 |
| `#DBE4EF` | 2 | static/style-part2.css:1618, static/style-part4.css:1031 |
| `#DBE5F0` | 1 | static/style-part2.css:1197 |
| `#DBE5F1` | 5 | static/ui-kit.css:1051, static/ui-kit.css:1067, static/ui-kit.css:1081, static/ui-kit.css:1171, static/ui-kit.css:1506 |
| `#DBE7F4` | 1 | static/style-part3.css:3517 |
| `#DBE8F5` | 9 | static/style-part4.css:3228, static/style-part4.css:3310 [comment], static/style-part4.css:3316, static/style-part4.css:3321, static/style-part4.css:3982, static/style-part4.css:4006, static/style-part4.css:4014, static/style-part4.css:4026 [comment], static/style-part4.css:4037 |
| `#DBE8F7` | 1 | static/ui-kit.css:2725 |
| `#DBEAFD` | 1 | static/ui-kit.css:3103 |
| `#DBEAFE` | 3 | static/style-part2.css:436, static/style-part2.css:593, static/style-part2.css:642 |
| `#DC2626` | 1 | static/style-part2.css:1512 |
| `#DCCFAD` | 1 | static/style-part1.css:922 |
| `#DCE3EC` | 2 | static/style-part2.css:1764, static/style-part2.css:1814 |
| `#DCE4ED` | 3 | static/style-part3.css:915, static/style-part3.css:1021, static/style-part3.css:1152 |
| `#DCE6F0` | 1 | static/ui-kit.css:1875 |
| `#DCE7F1` | 1 | static/style-part4.css:1046 |
| `#DCE8F5` | 1 | static/ui-kit.css:2204 |
| `#DCE8F6` | 1 | static/ui-kit.css:2620 |
| `#DCEAFB` | 1 | static/style-part2.css:908 |
| `#DDE6EF` | 1 | static/style-part3.css:1219 |
| `#DDE8F0` | 1 | static/style-part1.css:1738 |
| `#DF4B5A` | 1 | static/style-part2.css:3292 |
| `#DF5B64` | 4 | static/style-part2.css:3328, static/style-part2.css:3358, static/style-part4.css:3584 [comment], static/style-part4.css:3599 |
| `#DF5D68` | 3 | static/style-part4.css:614, static/style-part4.css:628, static/style-part4.css:763 |
| `#DF6B76` | 2 | static/style-part4.css:822, static/style-part4.css:829 |
| `#DFB3B3` | 1 | static/style-part2.css:929 |
| `#DFB5AF` | 1 | static/style-part2.css:1280 |
| `#DFE3EA` | 3 | static/style-part1.css:3027, static/style-part2.css:52, static/style-part2.css:145 |
| `#DFE4EB` | 6 | static/style-part2.css:2642, static/style-part2.css:2718, static/style-part2.css:2753, static/style-part3.css:191, static/style-part3.css:337, static/style-part3.css:344 |
| `#DFE5EC` | 8 | static/style-part3.css:632, static/style-part3.css:718, static/style-part3.css:749, static/style-part3.css:1799, static/style-part3.css:1827, static/style-part3.css:1853, static/style-part3.css:1881, static/style-part4.css:67 |
| `#DFE5EE` | 2 | static/style-part2.css:408, static/style-part2.css:736 |
| `#DFE7F2` | 1 | static/style-part3.css:229 |
| `#DFE8F1` | 1 | static/style-part3.css:843 |
| `#DFE9F4` | 1 | static/style-part3.css:3626 |
| `#DFF2E5` | 1 | static/style-part2.css:2521 |
| `#E05D68` | 1 | static/style-part4.css:2954 |
| `#E0B667` | 1 | static/style-part4.css:961 |
| `#E0C8B8` | 1 | static/style-part1.css:1096 |
| `#E0C8C0` | 1 | static/style-part1.css:933 |
| `#E0C98B` | 1 | static/style-part2.css:923 |
| `#E0E0E0` | 1 | static/style-part1.css:1502 |
| `#E0E6EE` | 1 | static/style-part2.css:3949 |
| `#E0E7EF` | 3 | static/style-part3.css:1217, static/style-part3.css:1218, static/style-part3.css:1326 |
| `#E0F2FE` | 1 | static/style-part2.css:655 |
| `#E0F3E5` | 1 | static/style-part2.css:2457 |
| `#E14D68` | 4 | static/style-part4.css:1990, static/style-part4.css:1991, static/style-part4.css:1992, static/style-part4.css:2293 |
| `#E1E6ED` | 2 | static/style-part3.css:3651, static/style-part3.css:3704 |
| `#E1E9F2` | 1 | static/ui-kit.css:3141 |
| `#E2BD45` | 1 | static/ui-kit.css:271 |
| `#E2E8F0` | 2 | static/style-part2.css:444, static/style-part2.css:524 |
| `#E2EBF5` | 2 | static/style-part3.css:674, static/ui-kit.css:3119 |
| `#E2F3E7` | 3 | static/style-part2.css:1731, static/style-part2.css:1894, static/style-part2.css:2314 |
| `#E3E8EE` | 2 | static/style-part4.css:129, static/style-part4.css:160 |
| `#E4EAF1` | 1 | static/ui-kit.css:3095 |
| `#E4EBF3` | 1 | static/ui-kit.css:47 |
| `#E4EDF7` | 2 | static/ui-kit.css:1146, static/ui-kit.css:3009 |
| `#E5E5E5` | 1 | static/style-part1.css:975 |
| `#E5E7EB` | 2 | static/style-part2.css:461, static/style-part2.css:1440 |
| `#E5E8EC` | 32 | static/style-part1.css:112, static/style-part1.css:358, static/style-part1.css:388, static/style-part1.css:477, static/style-part1.css:562, static/style-part1.css:654, static/style-part1.css:1907, static/style-part1.css:2071, static/style-part1.css:2116, static/style-part1.css:2128, static/style-part1.css:2188, static/style-part1.css:2213, static/style-part1.css:2387, static/style-part1.css:2472, static/style-part1.css:2534, static/style-part1.css:2624, static/style-part1.css:2636, static/style-part1.css:2703, static/style-part1.css:2722, static/style-part1.css:3102, static/style-part2.css:2055, static/style-part2.css:2069, static/style-part2.css:2135, static/style-part2.css:2143, static/style-part2.css:3520, static/style-part2.css:3575, static/style-part2.css:3664, static/style-part2.css:3740, static/style-part2.css:3824, static/style-part2.css:3893, static/style-part2.css:3989, static/style-part3.css:643 |
| `#E5EAF0` | 2 | static/ui-kit.css:863, static/ui-kit.css:3136 |
| `#E5EFF8` | 16 | static/style-part4.css:3216, static/style-part4.css:3222, static/style-part4.css:3252 [comment], static/style-part4.css:3262, static/style-part4.css:3401, static/style-part4.css:3410, static/style-part4.css:3425, static/style-part4.css:3974 [comment], static/style-part4.css:3981, static/style-part4.css:3988, static/style-part4.css:4002, static/style-part4.css:4013, static/style-part4.css:4021, static/style-part4.css:4026 [comment], static/style-part4.css:4034, static/style-part4.css:4035 |
| `#E6C765` | 1 | static/style-part3.css:256 |
| `#E6E9EF` | 3 | static/style-part1.css:2837, static/style-part1.css:2873, static/style-part2.css:1946 |
| `#E6ECF3` | 5 | static/style-part3.css:1078, static/style-part4.css:3704, static/style-part4.css:3844, static/style-part4.css:3873, static/style-part4.css:3938 |
| `#E6EEF8` | 1 | static/ui-kit.css:2517 |
| `#E7E7E7` | 1 | static/style-part2.css:482 |
| `#E7EBEF` | 1 | static/style-part2.css:1250 |
| `#E7EBF0` | 1 | static/style-part4.css:119 |
| `#E7EDF4` | 2 | static/style-part4.css:2037, static/style-part4.css:2353 |
| `#E7EDF5` | 1 | static/style-part3.css:3390 |
| `#E7EEF7` | 1 | static/ui-kit.css:1107 |
| `#E7EEF8` | 1 | static/style-part4.css:292 |
| `#E7F1FD` | 1 | static/style-part4.css:3485 |
| `#E8B437` | 3 | static/style-part2.css:3354, static/style-part4.css:3583 [comment], static/style-part4.css:3595 |
| `#E8B9BD` | 1 | static/style-part3.css:319 |
| `#E8C86A` | 1 | static/style-part1.css:1378 |
| `#E8D0CC` | 1 | static/style-part1.css:939 |
| `#E8D5D5` | 1 | static/style-part1.css:990 |
| `#E8DCC8` | 1 | static/style-part1.css:738 |
| `#E8DDC0` | 1 | static/style-part1.css:914 |
| `#E8DDC8` | 1 | static/style-part1.css:3501 |
| `#E8E0D0` | 1 | static/style-part1.css:2340 |
| `#E8E0D5` | 1 | static/style-part1.css:365 |
| `#E8E0F0` | 1 | static/style-part1.css:228 |
| `#E8E5E0` | 1 | static/style-part1.css:986 |
| `#E8EAED` | 8 | static/style-part1.css:77, static/style-part1.css:672, static/style-part1.css:1846, static/style-part1.css:2090, static/style-part1.css:2495, static/style-part1.css:2506, static/style-part1.css:2658, static/style-part2.css:3391 |
| `#E8ECF0` | 3 | static/style-part1.css:224, static/style-part1.css:463, static/style-part1.css:529 |
| `#E8EDF3` | 1 | static/style-part3.css:691 |
| `#E8EDF4` | 1 | static/ui-kit.css:896 |
| `#E8EEF5` | 2 | static/style-part2.css:1182, static/ui-kit.css:1883 |
| `#E8EEF7` | 3 | static/style-part1.css:3491, static/style-part2.css:162, static/ui-kit.css:2124 |
| `#E8F0F8` | 2 | static/style-part1.css:571, static/style-part3.css:60 |
| `#E8F0FE` | 2 | static/style-part2.css:3788, static/style-part2.css:3847 |
| `#E8F2FB` | 1 | static/style-part4.css:893 |
| `#E8F6EC` | 1 | static/style-part2.css:2582 |
| `#E9B53C` | 1 | static/style-part2.css:3287 |
| `#E9EDF2` | 2 | static/style-part3.css:769, static/ui-kit.css:3075 |
| `#E9EDF3` | 1 | static/style-part2.css:948 |
| `#E9F7ED` | 2 | static/style-part2.css:2512, static/style-part4.css:3341 [comment] |
| `#EAD0D0` | 1 | static/style-part3.css:726 |
| `#EAD8AE` | 1 | static/style-part3.css:1310 |
| `#EAF1F7` | 1 | static/style-part3.css:2901 |
| `#EAF2FB` | 3 | static/ui-kit.css:2138, static/ui-kit.css:2269, static/ui-kit.css:2270 |
| `#EAF2FC` | 1 | static/ui-kit.css:1006 |
| `#EAF2FF` | 1 | static/ui-kit.css:2112 |
| `#EAF3FB` | 3 | static/style-part2.css:2588, static/style-part4.css:578, static/style-part4.css:616 |
| `#EAF3FF` | 4 | static/style-part3.css:1814, static/style-part3.css:1868, static/ui-kit.css:3102, static/ui-kit.css:3115 |
| `#EAF4FF` | 2 | static/style-part4.css:624, static/style-part4.css:753 |
| `#EBC5C9` | 1 | static/ui-kit.css:276 |
| `#ECD0D0` | 1 | static/style-part1.css:2523 |
| `#ECEFF3` | 1 | static/style-part2.css:203 |
| `#EDC5C5` | 1 | static/style-part3.css:1304 |
| `#EDCC6A` | 1 | static/style-part1.css:928 |
| `#EDEBE7` | 1 | static/style-part1.css:778 |
| `#EDF0F3` | 1 | static/style-part4.css:139 |
| `#EDF0F4` | 4 | static/style-part1.css:3110, static/style-part1.css:3272, static/style-part2.css:2758, static/ui-kit.css:902 |
| `#EDF1F6` | 2 | static/style-part4.css:1341, static/ui-kit.css:1227 |
| `#EDF2F7` | 2 | static/style-part3.css:836, static/style-part4.css:3007 |
| `#EDF3F8` | 2 | static/style-part2.css:1238, static/style-part4.css:931 |
| `#EDF3F9` | 2 | static/style-part3.css:664, static/ui-kit.css:3118 |
| `#EDF3FB` | 4 | static/style-part1.css:2952, static/style-part2.css:2682, static/style-part3.css:1968, static/ui-kit.css:805 |
| `#EDF4FB` | 8 | static/style-part4.css:2035, static/style-part4.css:2397, static/style-part4.css:2803, static/style-part4.css:2831, static/ui-kit.css:1090, static/ui-kit.css:1129, static/ui-kit.css:2176, static/ui-kit.css:2197 |
| `#EDF4FF` | 1 | static/style-part4.css:102 |
| `#EDF5FF` | 6 | static/ui-kit.css:1213, static/ui-kit.css:1225, static/ui-kit.css:1226, static/ui-kit.css:1230, static/ui-kit.css:2029, static/ui-kit.css:2087 |
| `#EEF0F4` | 2 | static/style-part1.css:3040, static/style-part1.css:3060 |
| `#EEF1F6` | 1 | static/style-part2.css:2855 |
| `#EEF2F5` | 7 | static/style-part2.css:3449, static/style-part2.css:3638, static/style-part2.css:3720, static/style-part2.css:3770, static/style-part2.css:3870, static/style-part2.css:3923, static/style-part2.css:3941 |
| `#EEF2F7` | 3 | static/style-part2.css:144, static/style-part3.css:3619, static/ui-kit.css:3070 |
| `#EEF3F8` | 10 | static/style-part2.css:1035, static/style-part2.css:1642, static/style-part2.css:2576, static/style-part4.css:2030, static/style-part4.css:3108, static/style-part4.css:3493, static/ui-kit.css:38, static/ui-kit.css:3074, static/ui-kit.css:3135, static/ui-kit.css:3140 |
| `#EEF4F8` | 1 | static/style-part1.css:1734 |
| `#EEF4FA` | 4 | static/style-part3.css:917, static/style-part3.css:1023, static/style-part3.css:1154, static/style-part4.css:849 |
| `#EEF4FB` | 9 | static/style-part4.css:1242, static/style-part4.css:1276, static/style-part4.css:1539, static/style-part4.css:1553, static/style-part4.css:1658, static/style-part4.css:1687, static/style-part4.css:1729, static/style-part4.css:1760, static/style-part4.css:1789 |
| `#EEF4FF` | 1 | static/style-part1.css:1986 |
| `#EEF5FC` | 1 | static/ui-kit.css:1183 |
| `#EEF5FD` | 1 | static/ui-kit.css:2506 |
| `#EEF5FF` | 3 | static/style-part2.css:511, static/style-part2.css:583, static/ui-kit.css:2436 |
| `#EEF6FC` | 3 | static/style-part4.css:631, static/style-part4.css:851, static/style-part4.css:945 |
| `#EEF6FF` | 7 | static/ui-kit.css:1218, static/ui-kit.css:1973, static/ui-kit.css:1984, static/ui-kit.css:3252, static/ui-kit.css:3268, static/ui-kit.css:3280, static/ui-kit.css:3287 |
| `#EEF8F1` | 1 | static/style-part2.css:2302 |
| `#EEF9F1` | 2 | static/style-part2.css:2452, static/style-part4.css:3342 [comment] |
| `#EEFAF3` | 1 | static/style-part3.css:1295 |
| `#EF4444` | 3 | static/style-part1.css:1329, static/style-part2.css:1513, static/style-part4.css:3060 |
| `#EF4F5F` | 4 | static/style-part4.css:966, static/style-part4.css:967, static/style-part4.css:972, static/style-part4.css:1037 |
| `#EFB6B1` | 1 | static/style-part4.css:3505 |
| `#EFC8CC` | 1 | static/ui-kit.css:321 |
| `#F0C75E` | 1 | static/ui-kit.css:2126 |
| `#F0DDDA` | 1 | static/style-part1.css:931 |
| `#F0E8D0` | 1 | static/style-part1.css:1752 |
| `#F0EEEA` | 1 | static/style-part1.css:986 |
| `#F0F0F0` | 13 | static/style-part1.css:161, static/style-part1.css:296, static/style-part1.css:489, static/style-part1.css:510, static/style-part1.css:528, static/style-part1.css:554, static/style-part1.css:1144, static/style-part1.css:1186, static/style-part1.css:1217, static/style-part1.css:1231, static/style-part1.css:1234, static/style-part1.css:1927, static/style-part1.css:2550 |
| `#F0F2F5` | 10 | static/style-part1.css:13, static/style-part1.css:655, static/style-part1.css:1842, static/style-part1.css:1904, static/style-part1.css:1951, static/style-part1.css:2070, static/style-part1.css:2469, static/style-part1.css:2700, static/style-part2.css:3432, static/style-part4.css:3268 [comment] |
| `#F0F4F0` | 1 | static/style-part1.css:596 |
| `#F0F4F8` | 2 | static/style-part1.css:204, static/style-part2.css:3984 |
| `#F1F3F6` | 1 | static/style-part1.css:3077 |
| `#F1F8F3` | 1 | static/style-part2.css:1275 |
| `#F2385A` | 1 | static/style-part4.css:3629 |
| `#F2D17A` | 1 | static/ui-kit.css:2235 |
| `#F2D38C` | 1 | static/style-part2.css:1586 |
| `#F2DDDD` | 1 | static/style-part3.css:737 |
| `#F2F4F7` | 2 | static/style-part2.css:1910, static/style-part2.css:2321 |
| `#F2F7FB` | 2 | static/style-part3.css:2549, static/style-part3.css:2556 |
| `#F2F7FC` | 2 | static/style-part4.css:3335 [comment], static/ui-kit.css:3216 |
| `#F2F7FD` | 1 | static/ui-kit.css:2148 |
| `#F3B63F` | 1 | static/style-part1.css:3381 |
| `#F3F6F9` | 1 | static/style-part3.css:720 |
| `#F3F6FA` | 2 | static/style-part2.css:402, static/style-part2.css:726 |
| `#F3F6FB` | 1 | static/style-part1.css:3055 |
| `#F3F7FB` | 2 | static/style-part3.css:2926, static/style-part4.css:2068 |
| `#F3F8FF` | 1 | static/ui-kit.css:2650 |
| `#F4F7FA` | 2 | static/style-part3.css:3712, static/style-part4.css:3099 |
| `#F4F7FB` | 2 | static/style-part4.css:1399, static/ui-kit.css:3163 |
| `#F4F8FC` | 7 | static/style-part4.css:640, static/style-part4.css:3181, static/style-part4.css:3376, static/ui-kit.css:1708, static/ui-kit.css:1716, static/ui-kit.css:1822, static/ui-kit.css:3259 |
| `#F4F8FD` | 4 | static/style-part4.css:2034, static/style-part4.css:2408, static/style-part4.css:2794, static/style-part4.css:2841 |
| `#F4F8FF` | 7 | static/ui-kit.css:2244, static/ui-kit.css:2252, static/ui-kit.css:2262, static/ui-kit.css:2263, static/ui-kit.css:2457, static/ui-kit.css:2927, static/ui-kit.css:2933 |
| `#F4FBF6` | 1 | static/style-part2.css:1660 |
| `#F59B2F` | 1 | static/ui-kit.css:1221 |
| `#F59E0B` | 1 | static/style-part4.css:3055 |
| `#F5A623` | 1 | static/style-part4.css:341 |
| `#F5C542` | 5 | static/style-part2.css:2930, static/style-part2.css:2941, static/style-part2.css:3413, static/style-part3.css:34, static/style-part3.css:304 |
| `#F5D980` | 1 | static/style-part1.css:924 |
| `#F5DDDD` | 1 | static/style-part2.css:930 |
| `#F5EDCF` | 1 | static/style-part2.css:924 |
| `#F5EDE8` | 1 | static/style-part1.css:1095 |
| `#F5F0E8` | 1 | static/style-part1.css:363 |
| `#F5F3EF` | 1 | static/style-part1.css:778 |
| `#F5F5F5` | 3 | static/style-part1.css:974, static/style-part1.css:1240, static/style-part1.css:1276 |
| `#F5F7FA` | 16 | static/style-part1.css:406, static/style-part1.css:449, static/style-part1.css:1633, static/style-part1.css:2127, static/style-part1.css:2187, static/style-part1.css:2212, static/style-part1.css:2635, static/style-part1.css:2721, static/style-part2.css:3477, static/style-part2.css:3539, static/style-part3.css:293, static/style-part3.css:621, static/style-part3.css:3427, static/style-part3.css:3517, static/style-part4.css:1585, static/ui-kit.css:3071 |
| `#F5F7FB` | 2 | static/style-part1.css:2822, static/style-part2.css:2834 |
| `#F5F8FB` | 1 | static/style-part3.css:2252 |
| `#F5F8FC` | 1 | static/style-part2.css:3594 |
| `#F5F9FD` | 1 | static/style-part4.css:892 |
| `#F5FFE8` | 1 | static/style-part1.css:213 |
| `#F6F8FA` | 1 | static/style-part2.css:1263 |
| `#F6F8FB` | 3 | static/style-part2.css:51, static/style-part2.css:2674, static/ui-kit.css:1207 |
| `#F6F9FC` | 1 | static/style-part4.css:2932 |
| `#F7E5E5` | 1 | static/style-part3.css:847 |
| `#F7F9FB` | 2 | static/style-part3.css:41, static/style-part3.css:347 |
| `#F7F9FC` | 10 | static/style-part3.css:405, static/style-part3.css:1801, static/style-part3.css:1855, static/style-part4.css:2031, static/style-part4.css:2378, static/ui-kit.css:864, static/ui-kit.css:2558, static/ui-kit.css:3073, static/ui-kit.css:3125, static/ui-kit.css:3134 |
| `#F7FAFF` | 1 | static/style-part4.css:250 |
| `#F7FBFF` | 1 | static/style-part3.css:3075 |
| `#F8EDED` | 1 | static/style-part3.css:728 |
| `#F8F0D8` | 1 | static/style-part1.css:995 |
| `#F8F4E8` | 1 | static/style-part1.css:1748 |
| `#F8F5F5` | 1 | static/style-part1.css:990 |
| `#F8F6F2` | 1 | static/style-part1.css:1669 |
| `#F8F8F8` | 1 | static/style-part1.css:723 |
| `#F8F9FA` | 9 | static/style-part1.css:27, static/style-part1.css:561, static/style-part1.css:1503, static/style-part1.css:2412, static/style-part1.css:2561, static/style-part2.css:2054, static/style-part2.css:2068, static/style-part2.css:2134, static/style-part2.css:2142 |
| `#F8F9FC` | 1 | static/style-part1.css:1842 |
| `#F8FAFC` | 17 | static/style-part1.css:2942, static/style-part2.css:443, static/style-part2.css:633, static/style-part2.css:1028, static/style-part2.css:1364, static/style-part2.css:1599, static/style-part2.css:1801, static/style-part2.css:1870, static/style-part2.css:3571, static/style-part2.css:3678, static/style-part2.css:3802, static/style-part2.css:3884, static/style-part2.css:3907, static/style-part3.css:200, static/style-part3.css:1598, static/style-part4.css:162, static/ui-kit.css:2400 |
| `#F8FBFF` | 6 | static/style-part2.css:1620, static/style-part3.css:917, static/style-part3.css:1023, static/style-part3.css:1154, static/style-part4.css:3846, static/ui-kit.css:1711 |
| `#FAFBFC` | 5 | static/style-part1.css:315, static/style-part1.css:2104, static/style-part1.css:2612, static/style-part1.css:3273, static/style-part2.css:95 |
| `#FAFBFD` | 1 | static/ui-kit.css:997 |
| `#FAFFF5` | 1 | static/style-part1.css:209 |
| `#FBE8A6` | 1 | static/style-part3.css:254 |
| `#FBF5DF` | 1 | static/style-part2.css:2594 |
| `#FBFCFE` | 3 | static/style-part3.css:644, static/style-part3.css:1460, static/style-part4.css:1611 |
| `#FCD34D` | 1 | static/style-part4.css:1183 |
| `#FCECEA` | 1 | static/style-part2.css:2600 |
| `#FDF8E8` | 1 | static/style-part1.css:995 |
| `#FECACA` | 1 | static/style-part2.css:662 |
| `#FEE2E2` | 1 | static/style-part2.css:672 |
| `#FF5722` | 2 | static/style-part1.css:210, static/style-part1.css:275 |
| `#FF6262` | 1 | static/style-part1.css:3389 |
| `#FF6B35` | 2 | static/style-part1.css:1886, static/style-part1.css:1895 |
| `#FF6B6B` | 1 | static/style-part4.css:345 |
| `#FF7D7D` | 1 | static/ui-kit.css:2127 |
| `#FF858C` | 1 | static/style-part3.css:3562 |
| `#FF8A80` | 1 | static/style-part4.css:3204 |
| `#FF9DA4` | 1 | static/style-part4.css:3151 |
| `#FFB3BA` | 1 | static/ui-kit.css:2229 |
| `#FFB4BA` | 1 | static/style-part4.css:3527 |
| `#FFB5BC` | 1 | static/style-part4.css:975 |
| `#FFBBC0` | 1 | static/style-part4.css:3157 |
| `#FFD35A` | 1 | static/style-part3.css:3556 |
| `#FFE7E9` | 1 | static/style-part3.css:3545 |
| `#FFE8E8` | 2 | static/style-part2.css:2446, static/style-part2.css:3994 |
| `#FFE8EB` | 1 | static/style-part4.css:824 |
| `#FFF0EF` | 2 | static/style-part4.css:112, static/style-part4.css:3504 |
| `#FFF0F1` | 1 | static/style-part3.css:318 |
| `#FFF2C7` | 1 | static/style-part3.css:3539 |
| `#FFF2F3` | 1 | static/style-part4.css:3240 |
| `#FFF3F3` | 1 | static/style-part3.css:1305 |
| `#FFF4F2` | 1 | static/style-part2.css:1281 |
| `#FFF4F5` | 1 | static/style-part4.css:807 |
| `#FFF5F5` | 3 | static/style-part2.css:663, static/style-part2.css:2432, static/style-part4.css:3341 [comment] |
| `#FFF9EA` | 1 | static/style-part3.css:1311 |
| `#FFFAF0` | 1 | static/style-part2.css:1269 |
| `#FFFFFF` | 46 | static/style-part1.css:1824, static/style-part1.css:2833, static/style-part1.css:2957, static/style-part1.css:3026, static/style-part1.css:3098, static/style-part2.css:407, static/style-part2.css:526, static/style-part2.css:735, static/style-part2.css:1215, static/style-part2.css:1766, static/style-part2.css:1816, static/style-part2.css:3387, static/style-part3.css:207, static/style-part3.css:354, static/style-part3.css:361, static/style-part3.css:417, static/style-part3.css:422, static/style-part3.css:516, static/style-part3.css:1289, static/style-part3.css:1460, static/style-part3.css:1958, static/style-part3.css:1977, static/style-part3.css:3018, static/style-part4.css:1222, static/style-part4.css:2032, static/style-part4.css:2033, static/style-part4.css:2251, static/style-part4.css:2345, static/style-part4.css:2352, static/style-part4.css:2416, static/style-part4.css:2512, static/style-part4.css:2751, static/style-part4.css:2883, static/style-part4.css:3147, static/style-part4.css:3189, static/style-part4.css:3277 [comment], static/style-part4.css:3386, static/ui-kit.css:2065, static/ui-kit.css:2100, static/ui-kit.css:2211, static/ui-kit.css:2732, static/ui-kit.css:3072, static/ui-kit.css:3089, static/ui-kit.css:3126, static/ui-kit.css:3127, static/ui-kit.css:3133 |

## Addendum (Stage 4.1) — theme-family token values

_Added by the theme-registry Stage 4.1 PR (MeshCenter Sharp), not part of the original 2026-09-03 Stage 0 baseline snapshot above — this is a separately-dated, ongoing addendum, not a regeneration of that baseline._

The Stage 0 taxonomy (`semantic-status-candidate` / `ui-normalization-candidate` / the `mixed:` variant) predates any `--mc-*` tokens existing at all - it classifies *raw* HEX still leaking into component CSS that a later stage should migrate to a token. Starting with Stage 4 (new theme families - Sharp, Gunmetal, Alpine, Teal Dark, Teal Light), each stage's own PR introduces brand-new HEX values that are the opposite case: they *are* the canonical token declarations themselves (`html[data-theme-family="..."] { --mc-*: #... }`), plus their swatch-preview mirrors in the family picker CSS. Neither existing category fits, so this addendum uses a new `theme-family-token-value` verdict for them instead of force-fitting into `ui-normalization-candidate`.

Stage 5-8 should follow the same pattern: add their own family's new leaf-token HEX values here under this same verdict (extending the table below, or adding a dated sub-section if the distinction between stages matters later) rather than re-litigating whether a new category is warranted.

### Theme-family token value (Stage 4.1 - MeshCenter Sharp)

_14 unique values_

| HEX | Occurrences | Locations |
|---|---|---|
| `#087A57` | 2 | static/ui-kit.css:3395, static/ui-kit.css:3400 |
| `#1E1E1E` | 1 | static/ui-kit.css:3399 |
| `#2E2E2E` | 3 | static/ui-kit.css:992, static/ui-kit.css:3386, static/ui-kit.css:3398 |
| `#4C4C4C` | 1 | static/ui-kit.css:3387 |
| `#6B6B6B` | 1 | static/ui-kit.css:3388 |
| `#777E82` | 1 | static/ui-kit.css:3394 |
| `#B8B8B8` | 1 | static/ui-kit.css:3393 |
| `#D9D9D9` | 1 | static/ui-kit.css:3392 |
| `#D9FFF1` | 1 | static/ui-kit.css:3383 |
| `#E0E2E3` | 1 | static/ui-kit.css:3380 |
| `#E6E8E9` | 1 | static/ui-kit.css:3382 |
| `#ECEEEF` | 2 | static/ui-kit.css:990, static/ui-kit.css:3377 |
| `#F0F1F2` | 1 | static/ui-kit.css:3381 |
| `#F5F6F7` | 1 | static/ui-kit.css:3378 |

Not registered: `#4DFFBC` (Sharp's `accent-reference` role). It appears only inside the Stage 4.1 PR-description comment in `ui-kit.css` explaining why that role was left unmapped (no existing token reads a "large-fill-with-dark-content" role, and the source doc's design intent says mint must not become the primary button color) - it is not a declared value anywhere, so it does not belong in this registry. `check_new_hex.py --diff` will keep flagging it as a false positive on this branch specifically: its `--diff` mode strips single-line `/* */` comments only, and this explanatory comment spans multiple lines, so the mention isn't recognized as being inside a comment the way the whole-file mode (`check_new_hex.py static/ui-kit.css`, which handles multi-line comments correctly) does. Confirmed clean otherwise: whole-file mode reports 0 unregistered literals in `static/ui-kit.css` after this addendum.

_Line numbers above are a snapshot as of the Stage 4.1 merge; Stage 4.2 inserted comment lines above the Sharp block, shifting them - see the Stage 4.2 sub-section below for current line numbers of its own additions. Not re-verifying/updating the 4.1 rows here since the HEX values and their identity are what this registry tracks, not exact line numbers._

### Theme-family token value (Stage 4.2 - MeshCenter Sharp, state-color exceptions)

_2 new unique values (success/danger foreground+surface reuse 4.1's already-registered `#087A57`/`#D9FFF1` - see the Stage 4.2 PR for why: success's Foreground role is the same literal value as 4.1's `accent-graphic`, and its Surface role the same as `surface-selected`/`accent-soft`, both intentional per the source doc, not a new value)_

| HEX | Occurrences | Locations |
|---|---|---|
| `#B42332` | 1 | static/ui-kit.css:3405 |
| `#FFE9E9` | 1 | static/ui-kit.css:3406 |

Not registered (same reasoning as 4.1's `#4DFFBC`): source doc's success/danger `Base` (`#4DFFBC`, `#FF4D4D`) and `Border` (`#16C98D`, `#E03A43`) values. Neither has a real consumer to land on - `--mc-success`/`--mc-danger` are already fully committed to the `Foreground` role (the only one that keeps every consumer's text legible; see the Stage 4.2 PR/`ui-kit.css` comment for the contrast numbers), and there is no separate border token. Mentioned only in the Stage 4.2 PR-description comment, not declared anywhere.
