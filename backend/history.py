"""Registro de aplicaciones (issue #32).

Cada `apply` que cambia algo deja una entrada con lo que hizo: enlaces creados
o reapuntados, carpetas movidas y backups. Con ese registro `linker.undo`
puede revertir la última aplicación.

El registro vive en el home del usuario, no junto a cada carpeta: una
aplicación toca varios harnesses y, con alcance de proyecto, carpetas que
pueden no existir ya. Se conservan las últimas `MAX_APPLICATIONS`; los backups
de las más antiguas siguen en disco, solo se olvida su registro.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

HOME_ENV = "HARNESS_LINKER_HOME"
MAX_APPLICATIONS = 20
_VERSION = 1
_lock = threading.RLock()


def history_path() -> Path:
    base = os.environ.get(HOME_ENV)
    folder = Path(base) if base else Path.home() / ".harness-skills-linker"
    return folder / "history.json"


def _read() -> list[dict]:
    path = history_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    try:
        data = json.loads(raw)
        apps = data["applications"]
        if not isinstance(apps, list):
            raise TypeError
        return apps
    except (ValueError, KeyError, TypeError):
        # Un registro ilegible no se pisa: se aparta para poder revisarlo.
        aside = path.with_name(f"history.corrupt-{time.strftime('%Y%m%d-%H%M%S')}.json")
        try:
            os.replace(path, aside)
        except OSError:
            pass
        return []


def _write(apps: list[dict]) -> None:
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": _VERSION, "applications": apps}, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="history-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def new_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def record(application: dict) -> None:
    with _lock:
        apps = _read()
        apps.append(application)
        _write(apps[-MAX_APPLICATIONS:])


def last() -> dict | None:
    with _lock:
        apps = _read()
        return apps[-1] if apps else None


def replace_last(application_id: str, entries: list[dict]) -> None:
    """Deja en la última aplicación solo `entries`; sin entradas, la elimina."""
    with _lock:
        apps = _read()
        if not apps or apps[-1].get("id") != application_id:
            return
        if entries:
            apps[-1]["entries"] = entries
        else:
            apps.pop()
        _write(apps)


def summarize(application: dict | None) -> dict | None:
    if not application:
        return None
    return {
        "id": application["id"],
        "timestamp": application["timestamp"],
        "scope": application.get("scope"),
        "project_dir": application.get("project_dir"),
        "entries": len(application.get("entries", [])),
    }
