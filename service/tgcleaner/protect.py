"""Your protected chats: ids that no action may touch. Saved next to the session so it survives restarts."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .config import make_private

PROTECTED_FILE = "protected.json"
log = logging.getLogger("tgcleaner.protect")


def load_protected(data_dir: Path | None) -> set[str]:
    if data_dir is None:
        return set()
    try:
        data = json.loads((data_dir / PROTECTED_FILE).read_text(encoding="utf-8"))
        return {str(x) for x in data} if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def save_protected(data_dir: Path | None, ids: set[str]) -> None:
    if data_dir is None:
        return
    path = data_dir / PROTECTED_FILE
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(sorted(ids)), encoding="utf-8")
        make_private(tmp)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("could not save protected chats: %s", type(e).__name__)
