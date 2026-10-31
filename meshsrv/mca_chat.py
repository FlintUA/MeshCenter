"""MCA-UX1: how MCAttach control frames appear in the chat.

A transfer travels as `MCA1:` text messages (OFFER, acks, key exchange). They used to land in the chat history as
plain text. The server now marks them when they are stored:

  * `{"type": "OFFER"}`        an incoming offer: the chat shows one compact "File received" bubble;
  * `{"type": "CONTROL"}`      every other frame (acks, key exchange, cancel, ...): kept in the history, never shown,
                               never counted as unread, never the chat list preview;
  * `{"type": "FILE_SENT", "attachment_id": ...}`  our own transfer, noted when it is queued.

Pure functions, no state. Old messages stored before this change carry no marker: `ensure_marker()` adds it when the
history is read.
"""
from typing import Optional

from meshsrv.attachments import codec

OFFER = "OFFER"
CONTROL = "CONTROL"
FILE_SENT = "FILE_SENT"


def marker_for_text(text) -> Optional[dict]:
    """The marker for a received/stored text, or None when it is an ordinary message."""
    if not isinstance(text, str) or not text.startswith(codec.TEXT_PREFIX):
        return None
    try:
        message_type = codec.peek_message_type(codec.from_text(text))
    except Exception:  # any undecodable frame is still a control frame, never shown as raw text
        return {"type": CONTROL}
    return {"type": OFFER} if message_type == codec.MessageType.OFFER else {"type": CONTROL}


def ensure_marker(message: dict) -> dict:
    """Add the marker to a stored message that predates it (idempotent). Returns the same dict."""
    if "mca" not in message and message.get("kind") != "me":
        marker = marker_for_text(message.get("text"))
        if marker:
            message["mca"] = marker
    return message


def counts_as_unread(marker: Optional[dict]) -> bool:
    return not marker or marker.get("type") != CONTROL


def preview_text(marker: Optional[dict], text: str) -> Optional[str]:
    """The chat-list preview for a message: None means "leave the preview as it is"."""
    if not marker:
        return text
    if marker.get("type") == CONTROL:
        return None
    return "\U0001F4CE"  # a paperclip, no words: nothing to translate
