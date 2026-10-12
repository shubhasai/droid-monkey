"""Actions droid-monkey never takes on an app's live backend.

Exploring and attacking a real app means tapping its buttons, and some buttons act on the real world: creating an
account, logging in, publishing an edit, sending a message, paying. On Wikipedia an explorer filled the sign-up
form and created a real account. So every tap droid-monkey makes goes through `check`: a target whose label, content
description or resource id names such an action is refused, unless the developer said the backend is a test one
(`live_backend: false` in monkey.yaml) or listed that action in `allow_actions`. Steps the developer wrote
themselves (`entry_steps`, e.g. logging in with a test account) are trusted.
"""

import re

from .tools import ui

OUTWARD = re.compile(
    r"create[\s_-]*(an[\s_-]*)?account|sign[\s_-]*up|\bregister|log[\s_-]*in\b|\blogin|sign[\s_-]*in|"
    r"publish|submit|\bsend\b|\bpost\b|\bpay\b|payment|\bbuy\b|purchase|place[\s_-]*order|check[\s_-]*out|"
    r"\bdelete\b|\bdonate|subscribe|transfer|\bconfirm[\s_-]*(order|payment|purchase)",
    re.I)


def labels(node: ui.Node, screen: ui.Screen | None) -> list[str]:
    """What identifies a tap target to a person: its text, description, resource id, and the texts inside it."""
    out = [node.text, node.desc, node.rid.rsplit("/", 1)[-1]]
    if screen is not None:
        x1, y1, x2, y2 = node.bounds
        out += [n.text or n.desc for n in screen.nodes
                if (n.text or n.desc) and n is not node and x1 <= n.center[0] <= x2 and y1 <= n.center[1] <= y2]
    return [s for s in out if s]


def outward_reason(node: ui.Node, screen: ui.Screen | None, allow: list[str]) -> str | None:
    """The matched phrase if tapping `node` would act on the live backend and it is not allowed; else None."""
    found = labels(node, screen)
    if any(a.lower() in s.lower() for a in allow for s in found):
        return None
    for s in found:
        if not node.editable and (m := OUTWARD.search(s)):
            return f"'{s[:60]}' ({m.group(0).lower()})"
    return None
