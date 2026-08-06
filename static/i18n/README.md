# i18n catalogs — translator notes

This directory holds one JSON catalog per locale (`en.json`, `de.json`, `ru.json`,
`uk.json`), loaded at runtime by `static/i18n.js`. See the i18n architecture plan
for the overall design; this file is specifically the running list of terms and
patterns that need care when real translations land (Stage 6), so it doesn't
get rediscovered the hard way.

## Never translate these terms

Protocol/product names and technical terms — keep identical in every locale,
including `en.json` (they should never even be *keys* pointing at translated
text, just literal strings wherever they appear inside a sentence):

- **Meshtastic** — product/project name
- **Raspberry Pi** — hardware product name
- **LongFast** — the default Meshtastic channel name (proper noun, not "long and fast")
- **LoRa** — radio protocol name
- **Waypoint** — Meshtastic's own term for a saved map point (has a specific technical meaning in-app; "путевая точка"/"Wegpunkt"-style translations would disconnect it from the Meshtastic app's own vocabulary that users already know)
- **GPS**
- **BLE** (Bluetooth Low Energy)
- **PSK** — Pre-Shared Key, the channel encryption key; a technical protocol abbreviation, not an English phrase to translate

## Node IDs and MAC-like identifiers

Strings matching `!xxxxxxxx` (8 hex digits, e.g. `!756f9960`) are node
identifiers, not text. If a future bulk `data-i18n` pass or extraction script
ever touches markup containing one, exclude it — these must never be run
through the catalog/lookup pipeline.

## "Channel" — investigated, resolved into three distinct cases

This one was flagged as ambiguous, so it was actually checked against the
current code rather than guessed at:

1. **Standalone UI vocabulary — translate normally.**
   - The sidebar section heading "Channels" (`chat.channels_heading` in the
     catalog, `templates/index.html:216`).
   - The Waypoint composer's form label "Channel" (`templates/index.html:1922`,
     labeling the channel-picker `<select>`).
   - Ordinary sentences that use "channel" as a common noun, e.g. the Delete
     All DM confirmation text ("...The LongFast channel will remain...",
     `static/chat.js:8284`) — translate the whole sentence; "LongFast" inside
     it stays untranslated per the list above.

2. **Generic fallback noun — translatable, but currently English-only on the
   frontend.** When a channel has no real configured name, several places
   fall back to the literal word `"Channel"` used as a placeholder, formatted
   through the same `"{name} [{index}]"` pattern as real names (see
   `formatChannelIndexLabel()`, `static/chat.js:1918-1929`):
   `static/chat.js:1927`, `static/chat.js:4431`, `static/chat.js:4445`, and
   the matching option in `templates/index.html:1923`
   (`<option value="0">Channel [0]</option>`). This is just the common noun
   "channel" and can be translated like case 1 — **but** see the coupling
   below before touching it.

3. **Backend-generated fallback data — do not translate, do not treat as UI
   text.** `f"Channel {channel_index}"` is generated server-side as the
   `chats[chat_id]["name"]` value whenever a radio channel has no real name
   configured: `server.py:2401`, `server.py:3012`, `server.py:4743`,
   `api/api_chat.py:338`, `api/api_chat.py:570`. This is backend business
   data returned over the API, not markup `data-i18n` ever touches — but it's
   a landmine for later: `normalizeWaypointChannels()`
   (`static/chat.js:4444`) has a regex, `/^channel\s+\d+$/i`, that pattern-matches
   this *English* string specifically to collapse a redundant "Channel 3 [3]"
   down to "Channel [3]". If this backend string is ever localized (it isn't
   currently — the backend has no i18n awareness at all, see the "Backend
   errors" section of the architecture plan for why), or if case 2's frontend
   fallback text is translated without updating this regex in lockstep, the
   match silently breaks and a redundant, untranslated index leaks into the
   UI (e.g. "Kanal 3 [3]" instead of "Kanal [3]"). If this ever needs to
   become properly localized, route it like a Stage-4-style error code
   instead of baking English text into stored data: have the backend send a
   `{"index": N, "name_is_fallback": true}` signal and let the frontend
   render it via `I18N.t()`.

## Keeping this list current

This is a living document — when Stage 6 (actual translation) surfaces
another ambiguous or do-not-translate term, add it here rather than letting
the next person rediscover it.
