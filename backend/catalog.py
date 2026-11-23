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

    def global_path(self) -> Path:
        """Carpeta de skills a nivel de usuario. La variable de entorno, si está definida, manda."""
        if self.global_env_var:
            base = os.environ.get(self.global_env_var)
            if base:
                return Path(base).expanduser() / (self.global_env_subdir or "")
        return Path(self.global_template).expanduser()

    def project_path(self, project_dir: str | os.PathLike[str]) -> Path:
        """Carpeta de skills dentro de un directorio de proyecto."""
        return Path(project_dir) / self.project_template


def load_catalog(path: Path = CATALOG_PATH) -> list[Harness]:
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)
    harnesses = []
    for item in raw["harnesses"]:
        env = item.get("global_env") or {}
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
            )
        )
    ids = [h.id for h in harnesses]
    if len(ids) != len(set(ids)):
        raise ValueError("Catálogo con ids de harness duplicados")
    return harnesses
