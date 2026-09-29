"""
PersonalManager — alarms/appointments, weekly timetable, rsync backup
schedules and cron jobs, for the TrXi-Ctrl PM tab.

Thin async wrapper around pm_store.py — the same module the PersonalManager
GUI, Chameleon chat (pm_apply/pm_list) and the pm MCP server use, so the
phone sees and changes exactly what every other front end does.
pm_store.py is found via PM_STORE_DIR, else ../scripts beside this repo.
Passwords, documents and diary are never exposed.

The phone's paired token is already admin (it can open a terminal), so
rsync/cron changes here are no escalation — allow_commands=True.
"""

import asyncio
import importlib
import os
import sys
from pathlib import Path

_pm = None


def _store():
    global _pm
    if _pm is None:
        repo = Path(__file__).resolve().parent.parent
        for d in (os.environ.get("PM_STORE_DIR"), repo.parent / "scripts"):
            if d and (Path(d) / "pm_store.py").exists():
                sys.path.insert(0, str(d))
                _pm = importlib.import_module("pm_store")
                break
        else:
            raise ImportError("pm_store.py not found — set PM_STORE_DIR")
    return _pm


def _check(collection: str, allow_cron: bool = False) -> None:
    ok = list(_store().SCHEMAS) + (["cron"] if allow_cron else [])
    if collection not in ok:
        raise ValueError(f"collection must be one of: {', '.join(ok)}")


def _list(collection: str):
    pm = _store()
    if not collection:
        return {c: pm.list_items(c) for c in pm.SCHEMAS}
    _check(collection, allow_cron=True)
    return pm.list_cron() if collection == "cron" else pm.list_items(collection)


# Blocking file/crontab work runs in a thread so the gateway loop stays free.
async def list_items(collection: str = "") -> dict:
    return {"ok": True, "items": await asyncio.to_thread(_list, collection)}


async def add_item(collection: str, fields: dict) -> dict:
    _check(collection)
    return {"ok": True, "item": await asyncio.to_thread(_store().add_item, collection, **fields)}


async def update_item(collection: str, item_id: str, fields: dict) -> dict:
    _check(collection)
    return {"ok": True, "item": await asyncio.to_thread(
        _store().update_item, collection, item_id, **fields)}


async def remove_item(collection: str, item_id: str) -> dict:
    _check(collection)
    return {"ok": True, "item": await asyncio.to_thread(_store().remove_item, collection, item_id)}


async def apply_lines(lines: str) -> dict:
    res = await asyncio.to_thread(_store().apply_text, lines, True)
    return {"ok": bool(res) and all(r["op"] != "error" for r in res),
            "results": res, **({} if res else {"error": "no PM lines found"})}


async def set_cron_enabled(match: str, enabled: bool) -> dict:
    return {"ok": True, "changed": await asyncio.to_thread(
        _store().set_cron_enabled, match, enabled)}


def format_help() -> str:
    return _store().PM_FORMAT
