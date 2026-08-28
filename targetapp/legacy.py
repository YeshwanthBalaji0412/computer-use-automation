"""Helpers that make the markup authentically awful.

The single most important function here is :func:`ctl`. Real ASP.NET WebForms apps -
which is what an enormous amount of US credit-union back-office software actually is -
emit control ids like::

    ctl00_ContentPlaceHolder1_grdMembers_ctl03_lnkView

The ``ctl03`` segment is a positional index assigned during server-side control-tree
construction. It shifts when anything above it in the tree changes, and in many real
apps it differs between renders of the same logical screen.

We reproduce that: the index is randomised per render. The consequence is that **any
automation which records an element id, a CSS selector built from one, or an XPath that
depends on it, is dead on the second run.** That is not us being unfair - it is the
condition that makes semantic (role + accessible name) targeting the only honest answer,
and this app exists to prove the point rather than assert it.

What we do *not* remove is the accessible name. Every control still has visible,
human-readable text, a <label for>, an alt, or a title. That is also true to life: the
markup rots, but a human operator can still read the screen. That readable surface is
the only stable contract, which is exactly what the locator ladder targets.
"""

from __future__ import annotations

import random

#: Legacy apps love meaningless two-character class names.
JUNK_CLASSES = ["x", "f7", "tbl2", "c1", "gv", "hdr2", "pnl"]


def ctl(name: str, suffix: str = "", *, rng: random.Random | None = None) -> str:
    """Return a churning ASP.NET-style control id.

    The ``ctlNN`` index is random per call, so ids are unstable across renders by
    construction. Nothing in this codebase may depend on them.
    """
    r = rng or random
    idx = r.randint(0, 47)
    tail = f"_{suffix}" if suffix else ""
    return f"ctl00_ContentPlaceHolder1_{name}_ctl{idx:02d}{tail}"


def junk_class(rng: random.Random | None = None) -> str:
    r = rng or random
    return r.choice(JUNK_CLASSES)


def render_seed(request_id: str) -> random.Random:
    """A per-render RNG.

    Ids churn between renders but stay internally consistent within one page, so
    ``<label for>`` still points at its input. Legacy apps are hostile, not broken.
    """
    return random.Random(request_id)
