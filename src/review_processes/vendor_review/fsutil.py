"""Shared atomic, private-at-creation file persistence."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_private_atomic(path: Path, text: str) -> None:
    """Atomically persist ``text`` so it is never group/world-readable.

    The temporary file is created with mode 0600 (mkstemp), so unlike a
    write-then-chmod sequence there is no window in which other users on a
    shared server can read the content mid-write. The fsync plus atomic
    replace guarantees a reader sees either the previous file or the new
    one, and a failure leaves no temp residue.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
