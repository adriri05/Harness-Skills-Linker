"""Detección de harnesses y lectura de skills.

Solo lee el disco, nunca lo modifica.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from .catalog import Harness, load_catalog

Scope = Literal["global", "project"]
SkillKind = Literal["real", "symlink", "broken"]

SKILL_FILE = "SKILL.md"


@dataclass
class Skill:
    name: str
    path: str
    kind: SkillKind
    real_path: str | None
    link_target: str | None
    has_skill_md: bool


@dataclass
class HarnessScan:
    id: str
    name: str
    scope: Scope
    path: str
    dir_exists: bool
    binary_found: str | None
    verified: bool
    note: str | None
    skills: list[Skill] = field(default_factory=list)

    @property
    def detected(self) -> bool:
        return self.dir_exists or self.binary_found is not None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["detected"] = self.detected
        return data


def _is_link(path: Path) -> bool:
    """Symlink o, en Windows, junction."""
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


def _resolves_to_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        # Bucle de symlinks u otro fallo del sistema de archivos.
        return False


def _read_link(path: Path) -> str | None:
    try:
        return os.readlink(path)
    except OSError:
        return None


def scan_skill_dir(directory: Path) -> list[Skill]:
    """Lista las skills de una carpeta, ordenadas por nombre.

    Una skill es una subcarpeta (real o enlazada). Se ignoran los ficheros
    sueltos y las entradas ocultas (por ejemplo `.system`). Un enlace cuyo
    destino no existe, o forma un bucle, se devuelve como `broken`.
    """
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name.lower())
    except OSError:
        return []

    skills: list[Skill] = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        path = Path(entry.path)
        link = _is_link(path)
        if _resolves_to_dir(path):
            real = os.path.realpath(path)
            skills.append(
                Skill(
                    name=entry.name,
                    path=str(path),
                    kind="symlink" if link else "real",
                    real_path=real,
                    link_target=_read_link(path) if link else None,
                    has_skill_md=(Path(real) / SKILL_FILE).is_file(),
                )
            )
        elif link:
            skills.append(
                Skill(
                    name=entry.name,
                    path=str(path),
                    kind="broken",
                    real_path=None,
                    link_target=_read_link(path),
                    has_skill_md=False,
                )
            )
    return skills


def scan_harness(
    harness: Harness, scope: Scope = "global", project_dir: str | os.PathLike[str] | None = None
) -> HarnessScan:
    if scope == "project":
        if project_dir is None:
            raise ValueError("El alcance 'project' necesita project_dir")
        path = harness.project_path(project_dir)
    else:
        path = harness.global_path()

    binary = next((found for b in harness.binaries if (found := shutil.which(b))), None)
    dir_exists = _resolves_to_dir(path)
    return HarnessScan(
        id=harness.id,
        name=harness.name,
        scope=scope,
        path=str(path),
        dir_exists=dir_exists,
        binary_found=binary,
        verified=harness.verified,
        note=harness.note,
        skills=scan_skill_dir(path) if dir_exists else [],
    )


def scan_all(
    scope: Scope = "global",
    project_dir: str | os.PathLike[str] | None = None,
    harnesses: list[Harness] | None = None,
) -> list[HarnessScan]:
    return [scan_harness(h, scope, project_dir) for h in (harnesses or load_catalog())]
