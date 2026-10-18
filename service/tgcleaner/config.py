"""Local configuration and private data directory.

Everything sensitive (API hash, Telegram session) lives in the data directory,
which is created with owner-only permissions. Nothing here is ever sent to the
Chrome extension.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_PORT = 8765

# ID of the unpacked extension in ../extension. It is fixed by the public "key"
# in extension/manifest.json, so it is the same on every computer.
DEFAULT_EXTENSION_ID = "fdjdkkclgclbgkhlhlfbnjkjocfjjijn"

CONFIG_FILE = "config.json"
SESSION_NAME = "telegram"  # Telethon appends ".session"

_API_HASH_RE = re.compile(r"^[0-9a-f]{32}$")
_EXTENSION_ID_RE = re.compile(r"^[a-p]{32}$")


def data_dir() -> Path:
    override = os.environ.get("TGC_DATA_DIR")
    return Path(override).expanduser() if override else Path.home() / ".tgcleaner"


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, stat.S_IRWXU)  # 0700
    return path


def make_private(path: Path) -> None:
    """Restrict a file to the current user (no-op on Windows, where the user profile is already private)."""
    if os.name == "posix" and path.exists():
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600


def validate_api_id(value) -> int:
    try:
        api_id = int(str(value).strip())
    except ValueError:
        raise ValueError("API ID must be a number (from my.telegram.org).") from None
    if not 0 < api_id < 2**31:
        raise ValueError("API ID is out of range.")
    return api_id


def validate_api_hash(value: str) -> str:
    api_hash = str(value).strip().lower()
    if not _API_HASH_RE.match(api_hash):
        raise ValueError("API hash must be the 32-character hex string from my.telegram.org.")
    return api_hash


def validate_extension_id(value: str) -> str:
    ext_id = str(value).strip().lower()
    if not _EXTENSION_ID_RE.match(ext_id):
        raise ValueError("Extension ID must be 32 letters a-p (see chrome://extensions).")
    return ext_id


@dataclass
class Config:
    api_id: int
    api_hash: str
    port: int = DEFAULT_PORT
    extension_ids: list[str] = field(default_factory=lambda: [DEFAULT_EXTENSION_ID])
    # Pause between two leave requests. Telegram does not publish exact limits;
    # a couple of seconds keeps bulk leaving well clear of flood waits.
    leave_delay_seconds: float = 2.0
    # Pause between items of the read-only engagement scan (1-2 small requests per item).
    scan_delay_seconds: float = 1.0
    # Flood waits up to this long are waited out automatically (once per item).
    # Longer ones stop the batch so you can retry later.
    max_auto_flood_wait: int = 300

    def __post_init__(self):
        self.api_id = validate_api_id(self.api_id)
        self.api_hash = validate_api_hash(self.api_hash)
        self.port = int(self.port)
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        self.extension_ids = [validate_extension_id(x) for x in self.extension_ids]
        self.leave_delay_seconds = max(0.5, float(self.leave_delay_seconds))
        self.scan_delay_seconds = max(0.3, float(self.scan_delay_seconds))
        self.max_auto_flood_wait = max(0, int(self.max_auto_flood_wait))

    @property
    def allowed_origins(self) -> set[str]:
        return {f"chrome-extension://{x}" for x in self.extension_ids}


def config_path(base: Path | None = None) -> Path:
    return (base or data_dir()) / CONFIG_FILE


def session_path(base: Path | None = None) -> Path:
    return (base or data_dir()) / SESSION_NAME


def load_config(base: Path | None = None) -> Config | None:
    path = config_path(base)
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    known = {k: raw[k] for k in Config.__dataclass_fields__ if k in raw}
    return Config(**known)


def save_config(cfg: Config, base: Path | None = None) -> Path:
    directory = ensure_private_dir(base or data_dir())
    path = config_path(directory)
    # Write atomically so a crash never leaves a half-written config.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(cfg), fh, indent=2)
        make_private(Path(tmp))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    make_private(path)
    return path


MIN_TOKEN_LENGTH = 24


@dataclass
class CloudSettings:
    config: Config
    access_token: str | None
    demo: bool


def cloud_settings_from_env(env=None) -> CloudSettings:
    """Settings for a hosted deployment (e.g. Railway), read from environment variables.

    TG_API_ID, TG_API_HASH  your app credentials from my.telegram.org (not needed with TGC_DEMO=1)
    TGC_ACCESS_TOKEN        optional: long random secret the extension must send (no auth when unset)
    PORT                    port to listen on (set by Railway)
    TGC_EXTENSION_IDS       optional, comma-separated extension IDs (default: the pinned one)
    TGC_DEMO=1              fake data, no Telegram (to test a deployment)
    """
    env = os.environ if env is None else env
    token = (env.get("TGC_ACCESS_TOKEN") or "").strip() or None
    if token is not None and len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"TGC_ACCESS_TOKEN is optional, but if set it must be at least {MIN_TOKEN_LENGTH} characters.")
    demo = env.get("TGC_DEMO", "").strip().lower() in ("1", "true", "yes")
    ext_ids = [x for x in (env.get("TGC_EXTENSION_IDS") or "").replace(" ", "").split(",") if x] or [DEFAULT_EXTENSION_ID]
    port = int(env.get("PORT") or 8080)
    if demo:
        cfg = Config(api_id=1, api_hash="0" * 32, port=port, extension_ids=ext_ids,
                     leave_delay_seconds=0.5, scan_delay_seconds=0.3, max_auto_flood_wait=60)
    else:
        missing = [k for k in ("TG_API_ID", "TG_API_HASH") if not env.get(k)]
        if missing:
            raise ValueError(f"Missing environment variable(s): {', '.join(missing)} (from my.telegram.org).")
        cfg = Config(api_id=env["TG_API_ID"], api_hash=env["TG_API_HASH"], port=port, extension_ids=ext_ids)
        for key, attr in (("TGC_LEAVE_DELAY", "leave_delay_seconds"), ("TGC_SCAN_DELAY", "scan_delay_seconds")):
            if env.get(key):
                setattr(cfg, attr, max(0.3, float(env[key])))
    return CloudSettings(config=cfg, access_token=token, demo=demo)
