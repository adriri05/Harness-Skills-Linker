"""Carga del catálogo de harnesses y expansión de sus rutas."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("catalog.json")


@dataclass(frozen=True)
class Harness:
    id: str
    name: str
    global_template: str
    project_template: str
    binaries: tuple[str, ...]
    verified: bool
    global_env_var: str | None = None
    global_env_subdir: str | None = None
    note: str | None = None
    source: str | None = None
    instructions_global: str | None = None
    instructions_global_env_var: str | None = None
    instructions_global_env_subdir: str | None = None
    instructions_project: str | None = None
    instructions_source: str | None = None
    instructions_note: str | None = None

    def global_path(self) -> Path:
        """Carpeta de skills a nivel de usuario. La variable de entorno, si está definida, manda."""
        if self.global_env_var:
            base = os.environ.get(self.global_env_var)
            if base:
                return Path(base).expanduser() / (self.global_env_subdir or "")
        return Path(self.global_template).expanduser()

    def project_path(self, project_dir: str | os.PathLike[str]) -> Path:
        """Carpeta de skills dentro de un directorio de proyecto.

        Una carpeta personalizada no tiene plantilla de proyecto: su ruta es la misma en cualquier alcance.
        """
        if not self.project_template:
            return self.global_path()
        return Path(project_dir) / self.project_template


    def instructions_global_path(self) -> Path | None:
        """Archivo de instrucciones a nivel de usuario, o None si el harness no lo tiene como archivo."""
        if not self.instructions_global:
            return None
        if self.instructions_global_env_var:
            base = os.environ.get(self.instructions_global_env_var)
            if base:
                return Path(base).expanduser() / (self.instructions_global_env_subdir or "")
        return Path(self.instructions_global).expanduser()

    def instructions_project_path(self, project_dir: str | os.PathLike[str]) -> Path | None:
        """Archivo de instrucciones dentro de un proyecto, o None si el harness no lo tiene."""
        if not self.instructions_project:
            return None
        return Path(project_dir) / self.instructions_project


def load_catalog(path: Path = CATALOG_PATH) -> list[Harness]:
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)
    harnesses = []
    for item in raw["harnesses"]:
        env = item.get("global_env") or {}
        ins = item.get("instructions") or {}
        ins_env = ins.get("global_env") or {}
        harnesses.append(
            Harness(
                id=item["id"],
                name=item["name"],
                global_template=item["global"],
                project_template=item["project"],
                binaries=tuple(item.get("binaries", [])),
                verified=item.get("verified", False),
                global_env_var=env.get("var"),
                global_env_subdir=env.get("subdir"),
                note=item.get("note"),
                source=item.get("source"),
                instructions_global=ins.get("global"),
                instructions_global_env_var=ins_env.get("var"),
                instructions_global_env_subdir=ins_env.get("subdir"),
                instructions_project=ins.get("project"),
                instructions_source=ins.get("source"),
                instructions_note=ins.get("note"),
            )
        )
    ids = [h.id for h in harnesses]
    if len(ids) != len(set(ids)):
        raise ValueError("Catálogo con ids de harness duplicados")
    return harnesses
