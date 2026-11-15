"""Decode actual quick-feed status reports without guessing missing state."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass


@dataclass(frozen=True)
class QuickFeed:
    command: str | None
    status: str | None
    minutes: int | None


def decode_quick_feed(value: str | None) -> QuickFeed:
    unknown = QuickFeed(None, None, None)
    if type(value) is not str:
        return unknown
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        return unknown
    if (len(data) != 3 and not (len(data) == 4 and data[3] == 0)
            or base64.b64encode(data).decode('ascii') != value):
        return unknown
    command = {0: 'none', 16: 'start', 32: 'cancel', 48: 'now'}.get(data[0])
    status = {0: 'idle', 1: 'pending', 2: 'feeding', 3: 'ready', 4: 'failed'}.get(data[1])
    if command is None or status is None:
        return unknown
    return QuickFeed(command, status, data[2])
