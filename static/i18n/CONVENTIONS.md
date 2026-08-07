# i18n conventions — quick reference

Short, practical rules for anyone (human or Claude) adding or translating strings
in this project. For the do-not-translate glossary and case-by-case decisions
(e.g. "Channel"), see [README.md](README.md) in this directory.

## Rule: new user-facing code must use `I18N.t()`, never hardcoded English

Any new string shown to the user in `static/*.js` — a toast, a label, a confirm
dialog, an error fallback — must go through the runtime instead of being typed
as a literal:

```js
// Wrong
showToast('Node image saved', 'success');

// Right
showToast(window.I18N.t('node_manager.image_saved'), 'success');
```

For static markup in `templates/index.html`, use `data-i18n` / `data-i18n-placeholder`
/ `data-i18n-title` / `data-i18n-aria-label` attributes instead of hardcoding text,
following the existing pattern in that file. Keep an emoji prefix outside the
translated span, e.g. `📍 <span data-i18n="waypoints.title">Waypoints</span>`.

For counted strings (plurals), use `window.I18N.plural(key, count, { count })`
with a catalog entry shaped as `{ one, few, many, other }` — never hand-roll an
`n === 1 ? '' : 's'` ternary. `few`/`many` matter for ru/uk even when the en/de
entries repeat the same text in every branch.

Every new key must be added to **all four** catalogs (`en.json`, `de.json`,
`ru.json`, `uk.json`) in the same commit — the stage-by-stage rollout in this
project's history always validated exact key-parity across catalogs before
shipping (flatten each file, diff the key sets, must be empty) and checked for
`[[missing]]` markers live in the browser. Do the same for new work.

## Key structure: `namespace.action`

Dotted, flat-ish keys grouped by UI area, e.g. `nodes.request_telemetry`,
`waypoints.delete_all_confirm`, `settings.release_radio`, `camera.turn_off`.
Existing namespaces: `common`, `nav`, `node_panel`, `nodes`, `weather`, `chat`,
`camera`, `media`, `devices`, `node_manager`, `system`, `settings`, `modals`,
`waypoints`, `notifications`, `errors`.

- Reuse an existing key when the English text is identical in the new spot
  (e.g. `common.retry`, `nodes.unknown_node`, `waypoints.coordinates`) instead
  of minting a near-duplicate — check the target catalog before adding a key.
- Prefer a new key over overloading one when the English text differs even
  slightly (a past mistake in this project reused `node_panel.charts`
  ("Charts") for a modal titled "Telemetry" — wrong label, caught before
  shipping). Same-looking English words in different contexts are not always
  the same translation.
- Params use `{name}` placeholders, e.g. `"reference_prefix": "Reference: {name}"`,
  passed as `I18N.t('nodes.reference_prefix', { name })`.

## Tone by locale

- **German (de):** informal `du`, not `Sie` — matches the hobbyist/community
  character of Meshtastic. "Klicke", "Wähle", "Gib ein", never "Klicken Sie".
- **Russian (ru) / Ukrainian (uk):** neutral, slightly informal — imperative
  verb forms without an explicit "вы"/"ви" pronoun and without stiff
  official/canonical phrasing. Match the tone already in `ru.json`/`uk.json`,
  not a formal-register rewrite.
- **English (en)** is the source of truth; `de`/`ru`/`uk` values must be real
  translations, not stub copies of the English text (the README's
  "Localization" section explicitly warns against silently shipping English
  placeholders under a language selector — see there for the user-facing
  framing of this).

## Glossary

Never-translate terms (Meshtastic, Raspberry Pi, LongFast, LoRa, Waypoint,
GPS, BLE, PSK) and the fully-reasoned "Channel" case split live in
[README.md](README.md) — read it before translating anything ambiguous rather
than guessing.
