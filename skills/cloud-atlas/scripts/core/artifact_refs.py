"""Local-file guard: never hydrate a cloud placeholder (Issue #22).

This module is deliberately small.  It only answers one question - "may this path
be opened locally?" - for the paths the ``--only-file`` mode must read: the source
and the target filemap.  It does not discover references, does not walk directories
and does not follow links.
"""

from __future__ import annotations

import os

# FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
# A provider may hydrate on open (RECALL_ON_OPEN) instead of on data access, so every
# one of these attributes forbids the access just like a missing local copy.
_WINDOWS_PLACEHOLDER_ATTRIBUTES = 0x1000 | 0x40000 | 0x400000


class CloudPlaceholderError(OSError):
    """The path is a cloud placeholder; accessing it would hydrate it."""


def is_cloud_placeholder(path: str) -> bool:
    """True when a path must not be opened locally.

    A path that does not exist is a normal "missing" result.  An *existing* path
    whose attributes cannot be read is treated as a placeholder (fail-closed), so a
    stat error never silently turns into a local read.
    """
    if os.name != "nt":
        return False
    if not os.path.lexists(path):
        return False
    try:
        attributes = getattr(os.stat(path), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & _WINDOWS_PLACEHOLDER_ATTRIBUTES)
