# Privacy: Installation ID

This document covers exactly one thing: the **Installation ID** shown on MeshCenter's System card (`MC1-XXXX-XXXX-XXXX-XXXX-XXXX`). It is not a general privacy policy for the MeshCenter project as a whole.

## What it is

A random identifier generated once, the first time MeshCenter starts on a given install:

- Generated with Python's `secrets.token_hex()` - a cryptographically secure random number generator, the same one used to generate passwords and tokens elsewhere in this codebase.
- 20 hexadecimal characters, formatted as `MC1-XXXX-XXXX-XXXX-XXXX-XXXX` for readability.
- Stored locally in `data/instance.json`, alongside when it was assigned and how (see `meshsrv/instance_manager.py` if you want to read the actual code).

It identifies **this installation of the MeshCenter software** - one particular `data/` directory on one particular Raspberry Pi (or other host) at one particular point in time.

## What it is not

- **Not a hardware identifier.** It contains no MAC address, no CPU serial number, no radio serial number, no disk ID - nothing derived from the physical device it's running on. Moving MeshCenter to different hardware (a fresh SD card, a replacement Pi) and copying `data/` over would carry the same ID with it; conversely, wiping and reinstalling on the *same* hardware produces a brand new one. It tracks the install, not the machine.
- **Not tied to your Meshtastic radio.** Swapping which physical radio is connected to a given MeshCenter install (see the "Multi-radio profiles" section above) does not change this ID, and regenerating this ID does not affect the radio, its node ID, or anything about the mesh.
- **Not personal data by itself.** Nothing about you - name, location, email, anything you've typed into MeshCenter - is used to generate it or derivable from it; it is, deliberately, pure entropy with nothing meaningful folded in. That said, an identifier is only ever as anonymous as what it eventually gets linked to: if this ID were ever attached to something identifying (a support ticket with your name on it, an account, an email thread), it would become a pseudonymous identifier for you at that point, the same way any random reference number would. Nothing in MeshCenter does that linking today, but that's a statement about current behavior, not a property the ID carries on its own.

## Where it goes

**Nowhere, currently.** As of this writing, nothing in the MeshCenter codebase transmits the Installation ID anywhere:

- It is not sent to Meshtastic devices or over the mesh network.
- It is not included in any outbound network request MeshCenter makes. MeshCenter makes outbound requests for a few optional features - checking for software updates (`meshsrv/update_service.py`, against GitHub's public releases API), fetching weather data (`weather/providers/openweather.py`, `weather/providers/weatherapi.py`), and reverse geocoding a map reference point into a place name (`api/api_settings.py`, against OpenStreetMap's Nominatim). Checked each of these directly: none of them include the Installation ID, or any other instance-identifying value, in their request parameters or headers.
- It is not written to logs sent anywhere external, or included in exported node/chat data.

It exists today purely for local reference (visible on the System card, retrievable via `GET /api/instance`) and as groundwork for future tooling that may need to distinguish one MeshCenter installation from another - for example, correlating support requests or, eventually, an opt-in fleet-management feature. If a future feature ever does transmit this ID anywhere, that will be a deliberate, documented change to this file, not a silent one - and any such feature should be opt-in, not on by default.

## Regenerating it

You can generate a new Installation ID with the service stopped - see the [User Guide](docs/User_Guide.md#installation-id) for the command. This permanently replaces the old value; nothing links the new ID back to the old one. There's no server-side registry of issued IDs to notify or reconcile with - it's a purely local value, so regenerating it has no effect outside this one installation.
