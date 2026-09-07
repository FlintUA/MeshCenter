# MCAttach: Sending Files Over Meshtastic - Plain-Language Overview

**Status:** Living document, current as of the PR #231 review hardening pass.
**Audience:** anyone who wants to understand what MCAttach does and how it keeps files private, without reading code. For the technical version, see `MCAttach_File_Transfer_Technical.md`.

## The problem MCAttach solves

Meshtastic radios can only send tiny text messages - a couple hundred bytes at most. That is fine for chat, but useless for a photo, a document, or any real file. MCAttach lets two MeshCenter users send each other files anyway, without needing the mesh itself to carry the file's bytes.

## The basic idea

1. Instead of putting the file on the mesh, MeshCenter encrypts it and uploads the encrypted copy to a **Relay** - a small storage server reachable over the internet.
2. Over the mesh, MeshCenter only ever sends a short, signed message saying "there is a file waiting for you, here is where to get it, and here is proof it's really from me."
3. The other person's MeshCenter downloads the encrypted file from the Relay and decrypts it locally. The Relay itself never sees the unencrypted file, and never has the key needed to open it.

Think of the Relay as a locked mailbox at a shared address: MeshCenter drops a locked box there and tells the recipient over the mesh where the box is and what the combination looks like (mathematically - the actual key is never sent as plain text, even over the mesh). Only the intended recipient's device can actually unlock the box.

## Who can you send files to?

Only someone you have already exchanged Meshtastic messages with, and only after both sides have confirmed each other's identity once. The first time you talk to someone new, MeshCenter needs to learn their "signing key" - a bit like learning someone's handwriting well enough to recognize their signature. Until you explicitly confirm that key (a one-time, human action - MeshCenter never does this automatically), MeshCenter will not encrypt a file for them. This approach is called "trust on first use," and it is the same idea apps like Signal use for verifying a new contact.

If someone's key ever changes unexpectedly (their device was reset, or - in the worst case - someone is trying to impersonate them), MeshCenter notices the mismatch and asks a human to decide whether to trust the new key, rather than silently accepting it.

## What happens if the file's destination doesn't match who you trust?

MeshCenter double-checks that the address it's about to send an encrypted file to is the *same* address it originally learned and confirmed that person's key at. If those two don't match - for example, if something in the system tried to route a file meant for one trusted contact to a different address - MeshCenter refuses to send it, the same way it would refuse to send to someone it has never confirmed at all. This is a safeguard that was tightened during a recent security review of this feature.

## What about the Relay - do I have to trust it?

You choose which Relay(s) to use, the same deliberate, explicit way you choose which contact to trust - MeshCenter never invents a Relay address on its own, and it never follows a Relay address suggested by an incoming message. A Relay only ever stores encrypted bytes it cannot read, so even if a Relay's operator wanted to look, there is nothing readable to see. You can register more than one Relay, and MeshCenter keeps track of each one's availability separately - one Relay being down does not affect another. A Relay you turn off is never deleted (so its history stays visible), it just stops being used for new transfers immediately.

## What MCAttach does not do yet

This is an early stage of the feature (Stage 1 of the project's execution plan), so a few things are deliberately not built yet:

- **No file-transfer screen in the app yet.** The underlying machinery (encrypting, uploading, downloading, tracking delivery) works and is thoroughly tested, but there is no button in the MeshCenter web interface to actually pick a file and send it yet - that is planned as the next stage.
- **No automatic "delivered"/"downloaded" confirmation back to the sender.** The receiving side already tracks whether it downloaded a file, but that confirmation does not yet travel back over the mesh to update the sender's own view.
- **No automatic switching between Relays** if your usual one goes down - you would need to register and pick a different one yourself.
- **No group/broadcast file sharing to multiple people at once** in this version - it is one sender, one recipient.

## Why this matters if you're evaluating MeshCenter for sensitive use

- Files are encrypted end-to-end before they ever leave your device - the Relay server, whoever operates it, and anyone who intercepts the mesh traffic in between, never see the plaintext.
- Trust is explicit and human-confirmed, not automatic - MeshCenter will not send a file to someone it has not had its identity confirmed for, and it will not send a file to an address that does not match the identity it confirmed.
- Nothing in this feature will ever fetch a URL supplied by an incoming mesh message - the list of Relays it's willing to talk to is something you configured yourself, ahead of time, not something an attacker on the mesh could redirect it to.
