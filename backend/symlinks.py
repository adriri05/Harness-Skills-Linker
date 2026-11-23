"""Creación y borrado de enlaces de directorio con soporte para Windows.

En Windows crear symlinks exige Modo Desarrollador o privilegios de
administrador. Sin ellos se usa una junction, que no necesita permisos y
sirve para directorios locales.
"""

from __future__ import annotations

import functools
import os
import subprocess
import tempfile
from pathlib import Path

_IS_WINDOWS = os.name == "nt"
_NO_PRIVILEGE_WINERROR = 1314

WINDOWS_HELP = (
    "Windows no permite crear symlinks sin privilegios. Activa el Modo Desarrollador "
    "(Configuración > Privacidad y seguridad > Para desarrolladores) o ejecuta la "
    "aplicación como administrador, y vuelve a aplicar."
)


class LinkError(OSError):
    """No se pudo crear el enlace. El mensaje indica el paso a seguir."""


def is_link(path: str | os.PathLike[str]) -> bool:
    """Symlink o, en Windows, junction (también si está roto)."""
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


@functools.lru_cache(maxsize=1)
def can_create_symlinks() -> bool:
    """Comprueba si el proceso puede crear symlinks de directorio."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp, "target")
        target.mkdir()
        try:
            os.symlink(target, Path(tmp, "link"), target_is_directory=True)
        except (OSError, NotImplementedError):
            return False
        return True


def link_mode() -> str:
    """'symlink' si hay permiso; 'junction' si solo se pueden crear junctions (Windows)."""
    if can_create_symlinks():
        return "symlink"
    return "junction" if _IS_WINDOWS else "none"


def _create_junction(link: str, target: str) -> None:
    try:
        import _winapi

        _winapi.CreateJunction(target, link)
        return
    except (ImportError, AttributeError, OSError):
        pass
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", link, target], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or result.stdout.strip() or "mklink /J falló")


def make_dir_link(link: str | os.PathLike[str], target: str | os.PathLike[str]) -> str:
    """Crea `link` apuntando al directorio `target`. Devuelve 'symlink' o 'junction'."""
    link_s, target_s = str(link), os.path.abspath(target)
    try:
        os.symlink(target_s, link_s, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError) as exc:
        if not _IS_WINDOWS:
            raise LinkError(f"No se pudo crear el enlace {link_s}: {exc}") from exc
        first = exc
    try:
        _create_junction(link_s, target_s)
        return "junction"
    except OSError as exc:
        no_priv = getattr(first, "winerror", None) == _NO_PRIVILEGE_WINERROR
        detail = WINDOWS_HELP if no_priv else f"No se pudo crear el enlace {link_s}: {first}."
        raise LinkError(f"{detail} Fallback a junction falló: {exc}") from exc


def remove_link(path: str | os.PathLike[str]) -> None:
    """Borra un enlace (symlink o junction) sin tocar su destino. Rechaza carpetas reales."""
    if not is_link(path):
        raise LinkError(f"{path} no es un enlace; no se borra.")
    try:
        os.unlink(path)
    except OSError:
        os.rmdir(path)
