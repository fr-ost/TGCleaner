"""Command line: python -m tgcleaner {setup,serve,logout,reset}"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import shutil
import sys

from .config import (
    DEFAULT_EXTENSION_ID, DEFAULT_PORT, Config, config_path, data_dir, ensure_private_dir,
    load_config, save_config, session_path, validate_api_hash, validate_api_id, validate_extension_id,
)


def _ask(prompt: str, validate, secret: bool = False, default=None):
    while True:
        suffix = f" [{default}]" if default is not None else ""
        raw = (getpass.getpass if secret else input)(f"{prompt}{suffix}: ").strip()
        if not raw and default is not None:
            raw = str(default)
        try:
            return validate(raw)
        except ValueError as e:
            print(f"  {e}")


def cmd_setup(args) -> int:
    base = data_dir()
    existing = load_config(base)
    print("TG Cleaner setup")
    print("Get an API ID and API hash at https://my.telegram.org -> API development tools.")
    print(f"They are stored only on this computer, in {config_path(base)}.\n")
    api_id = _ask("API ID", validate_api_id, default=existing.api_id if existing else None)
    api_hash = _ask("API hash (input hidden)", validate_api_hash, secret=True)
    ext_default = existing.extension_ids[0] if existing else DEFAULT_EXTENSION_ID
    ext_id = _ask("Chrome extension ID (press Enter if you didn't change the manifest key)",
                  validate_extension_id, default=ext_default)
    cfg = Config(api_id=api_id, api_hash=api_hash, port=existing.port if existing else DEFAULT_PORT,
                 extension_ids=[ext_id])
    if existing:
        cfg.leave_delay_seconds = existing.leave_delay_seconds
        cfg.scan_delay_seconds = existing.scan_delay_seconds
        cfg.max_auto_flood_wait = existing.max_auto_flood_wait
    path = save_config(cfg, base)
    print(f"\nSaved {path}. Start the service and open the extension to log in.")
    return 0


def cmd_serve(args) -> int:
    from .server import run

    if os.name == "posix":
        os.umask(0o077)  # everything the service writes (session, scan cache) is readable by its owner only

    if args.cloud:
        return serve_cloud()
    base = data_dir()
    cfg = load_config(base)
    if args.demo:
        from .fake_backend import FakeBackend

        demo_cfg = Config(api_id=1, api_hash="0" * 32,
                          port=cfg.port if cfg else DEFAULT_PORT,
                          extension_ids=cfg.extension_ids if cfg else [DEFAULT_EXTENSION_ID],
                          leave_delay_seconds=0.5, scan_delay_seconds=0.3, max_auto_flood_wait=60)
        if args.port:
            demo_cfg.port = args.port
        run(demo_cfg, FakeBackend(random_failures=True), demo=True)
        return 0
    if cfg is None:
        print("Not set up yet. Run: python -m tgcleaner setup", file=sys.stderr)
        return 2
    if args.port:
        cfg.port = args.port
    from .telegram_backend import TelethonBackend

    ensure_private_dir(base)
    run(cfg, TelethonBackend(cfg, base))
    return 0


def serve_cloud() -> int:
    """Hosted mode (Railway etc.): settings from environment variables, access token required."""
    from .config import cloud_settings_from_env
    from .server import run

    try:
        settings = cloud_settings_from_env()
    except ValueError as e:
        print(f"Cannot start: {e}", file=sys.stderr, flush=True)
        return 2
    base = ensure_private_dir(data_dir())
    if settings.demo:
        from .fake_backend import FakeBackend

        backend = FakeBackend(random_failures=True)
    else:
        from .telegram_backend import TelethonBackend

        backend = TelethonBackend(settings.config, base)
    print(f"Data directory: {base} (mount a persistent volume here, or you'll have to log in after every deploy)",
          flush=True)
    run(settings.config, backend, demo=settings.demo, access_token=settings.access_token, cloud=True)
    return 0


def cmd_logout(args) -> int:
    from .telegram_backend import TelethonBackend

    base = data_dir()
    cfg = load_config(base)
    if cfg is None:
        print("Not set up; nothing to log out.")
        return 0

    async def go():
        backend = TelethonBackend(cfg, base)
        await backend.start()
        result = await backend.log_out()
        await backend.stop()
        return result

    result = asyncio.run(go())
    if result["telegram_confirmed"]:
        print("Logged out. Telegram ended the session and the local session file was deleted.")
    else:
        print("Local session file deleted. Telegram could not confirm the logout; if the session still\n"
              "shows in Telegram > Settings > Devices, terminate it there.")
    return 0


def cmd_reset(args) -> int:
    base = data_dir()
    if not base.exists():
        print("Nothing to delete.")
        return 0
    if session_path(base).with_suffix(".session").exists():
        print("A Telegram session exists. Run 'logout' first so Telegram ends it too, or terminate it\n"
              "later in Telegram > Settings > Devices.")
    if input(f"Delete {base} (config, API credentials, session)? Type 'yes': ").strip() != "yes":
        print("Cancelled.")
        return 1
    shutil.rmtree(base)
    print("Deleted.")
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(prog="tgcleaner", description="Personal Telegram group cleaner (local service)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="store your Telegram API ID / hash locally")
    serve = sub.add_parser("serve", help="run the local service for the extension")
    serve.add_argument("--demo", action="store_true", help="fake data, no Telegram connection")
    serve.add_argument("--port", type=int, help=f"port (default {DEFAULT_PORT})")
    serve.add_argument("--cloud", action="store_true",
                       help="hosted mode (e.g. Railway): config from environment variables, access token required")
    sub.add_parser("logout", help="log out of Telegram and delete the local session")
    sub.add_parser("reset", help="delete all local data")
    args = parser.parse_args(argv)
    try:
        return {"setup": cmd_setup, "serve": cmd_serve, "logout": cmd_logout, "reset": cmd_reset}[args.command](args)
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
