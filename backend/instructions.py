"""Enlace de archivos de instrucciones entre harnesses (issue #33).

Cada harness lee un archivo distinto (`AGENTS.md`, `CLAUDE.md`, `GEMINI.md`). Aquí se
enlazan a un archivo canónico con las mismas garantías que las skills: plan antes de
tocar el disco, backup antes de sustituir un archivo real y nunca se sobrescribe contenido.

Solo entra el archivo único de cada harness. Las carpetas de reglas con varios archivos
y cabecera propia (por ejemplo `.cursor/rules`) quedan fuera. Dos harnesses que leen la
misma ruta (`AGENTS.md` en un proyecto) forman una sola fila del plan.
"""

from __future__ import annotations

import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import history
from .catalog import Harness, load_catalog
from .linker import (
    BACKUP_DIR,
    ApplyResult,
    Plan,
    PlanAction,
    UndoAction,
    _backup_path,
    _broken_reason,
    _missing_dirs,
    _norm,
    _raw_link,
    _tidy_backup_dirs,
    _rmdir_if_empty,
    _validate_rule,
)
from .symlinks import LinkError, can_create_symlinks, is_link, make_file_link, remove_link

FileId = tuple


@dataclass
class _File:
    path: Path
    kind: str  # real | symlink | broken | invalid
    file_id: Optional[FileId] = None
    real_path: Optional[str] = None
    hardlinked: bool = False
    detail: str = ""


@dataclass
class _Group:
    """Una ruta de archivo y los harnesses que la leen."""
    path: Path
    ids: list[str]


# ---------------------------------------------------------------- utilidades

def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _identity(st: os.stat_result, real: str) -> FileId:
    """Mismo archivo aunque haya enlaces duros o simbólicos de por medio."""
    if st.st_ino:
        return (st.st_dev, st.st_ino)
    return ("path", os.path.normcase(real))


def _inspect_file(path: Path) -> Optional[_File]:
    if not os.path.lexists(path):
        return None
    link = is_link(path)
    try:
        st = os.stat(path)
    except OSError as exc:
        if link:
            return _File(path, "broken")
        return _File(path, "invalid", detail=f"no se puede leer ({exc.strerror or type(exc).__name__})")
    if not stat.S_ISREG(st.st_mode):
        return _File(path, "invalid", detail="no es un archivo")
    real = os.path.realpath(path)
    return _File(path, "symlink" if link else "real", _identity(st, real), real,
                 hardlinked=not link and st.st_nlink > 1)


def _same_file(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _file_path(h: Harness, scope: str, project_dir: str | None) -> Optional[Path]:
    if scope == "project":
        if not project_dir:
            raise ValueError("El alcance 'project' necesita project_dir")
        return h.instructions_project_path(project_dir)
    if scope != "global":
        raise ValueError(f"Alcance desconocido: {scope}")
    return h.instructions_global_path()


def _catalog(harnesses: list[Harness] | None) -> dict[str, Harness]:
    return {h.id: h for h in (harnesses if harnesses is not None else load_catalog())}


# --------------------------------------------------------------------- scan

def scan(scope: str = "global", project_dir: str | None = None,
         harnesses: list[Harness] | None = None) -> list[dict]:
    """Estado del archivo de instrucciones de cada harness del catálogo. Solo lee el disco."""
    out = []
    for h in _catalog(harnesses).values():
        path = _file_path(h, scope, project_dir)
        binary = next((found for b in h.binaries if (found := shutil.which(b))), None)
        item = {
            "id": h.id, "name": h.name, "scope": scope,
            "path": str(path) if path else None,
            "kind": "none", "link_target": None, "hardlinked": False, "detail": "",
            "binary_found": binary, "source": h.instructions_source, "note": h.instructions_note,
        }
        if path:
            f = _inspect_file(path)
            item["kind"] = f.kind if f else "missing"
            item["hardlinked"] = bool(f and f.hardlinked)
            item["detail"] = f.detail if f else ""
            if f and f.kind in ("symlink", "broken"):
                try:
                    item["link_target"] = os.readlink(path)
                except OSError:
                    pass
        item["detected"] = bool(binary) or item["kind"] not in ("none", "missing")
        out.append(item)
    return out


# --------------------------------------------------------------------- plan

def build_plan(
    harness_ids: list[str],
    scope: str = "global",
    project_dir: str | None = None,
    canonical: str | None = None,
    conflict_rule: str = "none",
    harnesses: list[Harness] | None = None,
) -> Plan:
    _validate_rule(conflict_rule)
    plan = Plan()
    available = _catalog(harnesses)

    groups: dict[str, _Group] = {}
    seen_ids: set[str] = set()
    for hid in harness_ids:
        if hid in seen_ids:
            continue
        seen_ids.add(hid)
        if hid not in available:
            raise ValueError(f"Harness desconocido: {hid}")
        path = _file_path(available[hid], scope, project_dir)
        if path is None:
            plan.warnings.append(
                f"{available[hid].name} no tiene un archivo de instrucciones en el alcance {scope}. Se omite."
            )
            continue
        groups.setdefault(_key(path), _Group(path, [])).ids.append(hid)
    order = list(groups)

    canon_path: Optional[Path] = None
    if canonical:
        if canonical in available:
            canon_path = _file_path(available[canonical], scope, project_dir)
            if canon_path is None:
                raise ValueError(
                    f"{available[canonical].name} no tiene un archivo de instrucciones en el alcance {scope}"
                )
        else:
            expanded = Path(canonical).expanduser()
            if not expanded.is_absolute():
                raise ValueError(
                    f"Canonical debe ser un id de harness o una ruta absoluta: {canonical}"
                )
            canon_path = expanded
        plan.canonical = str(canon_path)
    canon_key = _key(canon_path) if canon_path else None

    if conflict_rule in ("prefer_canonical", "move_to_canonical", "move_all_to_canonical") and not canon_path:
        plan.warnings.append(f"La regla {conflict_rule} necesita un archivo canónico. Sin él, los conflictos quedan sin resolver.")
    if conflict_rule.startswith("prefer_harness:"):
        pid = conflict_rule.split(":", 1)[1]
        if pid not in seen_ids:
            plan.warnings.append(f"La regla prefiere '{pid}', que no está entre los harnesses seleccionados.")

    if os.name == "nt" and not can_create_symlinks():
        plan.warnings.append(
            "Sin privilegios, Windows no permite symlinks de archivo. Se crearán enlaces duros, que solo "
            "valen dentro del mismo volumen y que algunos editores rompen al guardar: el archivo pasa "
            "a ser una copia. Con el Modo Desarrollador se usan symlinks."
        )

    ents: dict[str, Optional[_File]] = {k: _inspect_file(g.path) for k, g in groups.items()}
    if canon_path and canon_key not in ents:
        ents[canon_key] = _inspect_file(canon_path)

    good = {k: f for k, f in ents.items() if f and f.kind in ("real", "symlink")}
    cands: dict[FileId, list[str]] = {}
    for k, f in good.items():
        cands.setdefault(f.file_id, []).append(k)
    multiple = len(cands) > 1
    canon_file = good.get(canon_key) if canon_key else None

    src: Optional[str] = None
    winner: Optional[str] = None  # grupo cuyo archivo real se mueve a la ruta canónica
    if canon_file:
        src = canon_file.real_path
    elif len(cands) == 1:
        src = next(iter(good.values())).real_path
    if multiple and conflict_rule.startswith("prefer_harness:"):
        pref_id = conflict_rule.split(":", 1)[1]
        pref_key = next((k for k, g in groups.items() if pref_id in g.ids), None)
        if pref_key in good:
            src = good[pref_key].real_path
    elif canon_path and not canon_file and (
        conflict_rule == "move_all_to_canonical" or (multiple and conflict_rule == "move_to_canonical")
    ):
        winner = next((k for k in order if k in good and good[k].kind == "real"), None)
        if winner:
            src = str(canon_path)
    conflict = multiple and src is None
    resolution = conflict_rule if multiple and conflict_rule != "none" else None

    rows: list[PlanAction] = []

    def row(key, action, reason, source=None, res=resolution, **flags):
        g = groups[key]
        if len(g.ids) > 1:
            reason += f" Lo leen también: {', '.join(g.ids[1:])}."
        rows.append(PlanAction(g.path.name, g.ids[0], str(g.path), source, action, reason, res, **flags))

    for key in order:
        s = ents[key]
        if s is not None and s.kind == "invalid":
            row(key, "skipped", f"{s.path.name} {s.detail}; no se toca.")
            continue
        if conflict:
            if s is None:
                row(key, "skipped", "Pendiente de resolver el conflicto.")
            elif s.kind == "broken":
                row(key, "skipped", f"{_broken_reason(str(s.path)).capitalize()}; pendiente del conflicto.")
            else:
                row(key, "conflict", "Existe en varios harnesses con contenido distinto.")
            continue
        if src is None:
            why = _broken_reason(str(s.path)).capitalize() if s else "Sin origen válido"
            row(key, "skipped", f"{why} y no hay origen válido para enlazar.")
            continue
        if winner and s is None and key == canon_key:
            row(key, "skipped", f"Recibe el archivo movido desde {groups[winner].ids[0]}.")
            continue
        if s is None:
            row(key, "create_link", "Falta en este harness.", src)
        elif s.kind == "broken":
            row(key, "create_link", f"Sustituye un enlace: {_broken_reason(str(s.path))}.", src, replace_link=True)
        elif key == winner:
            row(key, "create_link", f"Mueve el archivo a {src} (con backup) y lo enlaza.", src, move_to=src)
        elif s.file_id == _identity_of(src):
            if s.kind == "symlink":
                row(key, "already_linked", "Ya enlazado al origen.", src)
            elif _norm(str(s.path)) == _norm(src):
                row(key, "already_linked", "Archivo origen.", src)
            else:
                row(key, "already_linked", "Ya enlazado al origen con un enlace duro.", src)
        elif s.kind == "symlink":
            row(key, "create_link", f"Reapunta el enlace desde {s.real_path}.", src, replace_link=True)
        elif conflict_rule in ("move_to_canonical", "move_all_to_canonical") and multiple:
            row(key, "create_link", "Archivo real duplicado. Se hace backup y se enlaza al origen.", src, backup=True)
        elif multiple and conflict_rule != "none":
            row(key, "skipped", f"Se conserva el archivo real porque la regla {conflict_rule} no lo sustituye.", src)
        else:
            row(key, "conflict", "Archivo real distinto del archivo canónico.", src, res=None)
    if winner:
        rows.sort(key=lambda r: _key(Path(r.target)) != winner)
    plan.actions = rows
    return plan


def _identity_of(path: str) -> Optional[FileId]:
    try:
        return _identity(os.stat(path), os.path.realpath(path))
    except OSError:
        return None


# ------------------------------------------------------------------- apply

def apply(
    harness_ids: list[str],
    scope: str = "global",
    project_dir: str | None = None,
    canonical: str | None = None,
    conflict_rule: str = "none",
    harnesses: list[Harness] | None = None,
) -> ApplyResult:
    plan = build_plan(harness_ids, scope, project_dir, canonical, conflict_rule, harnesses)
    result = ApplyResult()
    entries: list[dict] = []
    for a in plan.actions:
        if a.action == "create_link":
            entry = {"skill": a.skill, "harness_id": a.harness_id, "item": "file"}
            try:
                status, message = _execute(a, entry)
            except LinkError as exc:
                status, message = "error", str(exc)
            except OSError as exc:
                status, message = "error", f"{type(exc).__name__}: {exc}"
            if entry.get("changed"):
                entries.append(entry)
        else:
            status, message = "skipped", a.reason
        result.results.append({**a.to_dict(), "status": status, "message": message})
    if entries:
        result.application_id = history.new_id()
        history.record({
            "id": result.application_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "scope": scope,
            "project_dir": project_dir,
            "canonical": plan.canonical,
            "conflict_rule": conflict_rule,
            "entries": entries,
        })
    return result


def _execute(a: PlanAction, entry: dict) -> tuple[str, str]:
    target, src = Path(a.target), a.source
    notes: list[str] = []
    entry.update(target=a.target, source=src, link_created=False, created_dirs=[])

    if a.move_to:
        if is_link(target) or not target.is_file():
            return "error", "El archivo a mover ya no es un archivo real."
        dest = Path(a.move_to)
        if os.path.lexists(dest):
            return "error", f"{dest} ya existe; no se sobrescribe."
        backup = _backup_path(target)
        shutil.copy2(target, backup)
        entry.update(backup=str(backup), changed=True)
        entry["created_dirs"] += _missing_dirs(dest.parent)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(dest))
        entry["moved_to"] = str(dest)
        notes.append(f"movido a {dest}, backup en {backup}")
    elif not src or not os.path.isfile(src):
        return "error", f"El origen {src} no existe."

    if os.path.lexists(target):
        if is_link(target):
            if os.path.isfile(target) and _same_file(target, src):
                return "skipped", "Ya enlazado."
            if not a.replace_link:
                return "error", "Ya hay un enlace distinto. No se sustituye."
            entry["previous_link"] = _raw_link(target)
            remove_link(target)
            entry["changed"] = True
        elif target.is_file() and a.backup:
            backup = _backup_path(target)
            os.rename(target, backup)
            entry.update(backup=str(backup), changed=True)
            notes.append(f"backup en {backup}")
        else:
            return "error", "Ya hay una carpeta o un archivo real. No se sobrescribe."

    entry["created_dirs"] += _missing_dirs(target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    kind = make_file_link(target, src)
    entry.update(kind=kind, link_created=True, changed=True)
    notes.append("enlace duro creado" if kind == "hardlink" else "symlink creado")
    text = "; ".join(notes)
    return "ok", text[0].upper() + text[1:] + "."


# -------------------------------------------------------------------- deshacer

def inspect_undo(e: dict) -> UndoAction:
    """Qué haría deshacer esta entrada de archivo, sin tocar el disco."""
    target = Path(e["target"])
    backup = e.get("backup")
    where = f" El backup sigue en {backup}." if backup and os.path.lexists(backup) else ""

    if e.get("link_created"):
        if not os.path.lexists(target):
            return UndoAction(e, "skipped", "El enlace ya no existe; no se toca." + where)
        if e.get("kind") == "hardlink":
            if is_link(target) or not _same_file(target, e["source"]):
                return UndoAction(e, "skipped",
                                  "Ya no es el mismo archivo que el origen; no se toca." + where)
        elif not is_link(target):
            return UndoAction(e, "skipped", "Ya no es un enlace; no se toca." + where)
        elif _norm(str(target)) != _norm(e["source"]):
            return UndoAction(e, "skipped",
                              f"El enlace apunta ahora a {_norm(str(target))}; no se toca." + where)
    elif os.path.lexists(target):
        return UndoAction(e, "conflict", "El destino ya existe; no se sobrescribe." + where)

    if e.get("moved_to"):
        dest = Path(e["moved_to"])
        if is_link(dest) or not dest.is_file():
            return UndoAction(e, "skipped", f"El archivo movido ya no está en {dest}; no se toca." + where)
        return UndoAction(e, "restore", f"Quita el enlace y devuelve el archivo desde {dest}.")
    if backup:
        if not os.path.lexists(backup):
            return UndoAction(e, "skipped",
                              f"El backup {backup} ya no existe y no se puede restaurar. El enlace se deja como está.")
        return UndoAction(e, "restore", f"Quita el enlace y restaura el archivo desde {backup}.")
    if e.get("previous_link"):
        return UndoAction(e, "restore", f"Quita el enlace y recrea el anterior hacia {e['previous_link']}.")
    return UndoAction(e, "remove_link", "Quita el enlace creado.")


def _restore_link(target: Path, raw: str) -> None:
    dest = raw if os.path.isabs(raw) else os.path.join(target.parent, raw)
    try:
        os.symlink(raw, target)
    except (OSError, NotImplementedError):
        make_file_link(target, dest)


def run_undo(ua: UndoAction) -> tuple[str, str]:
    e = ua.entry
    if ua.action in ("skipped", "conflict"):
        return ua.action, ua.reason
    target = Path(e["target"])
    notes: list[str] = []

    if e.get("link_created") and os.path.lexists(target):
        if is_link(target):
            remove_link(target)
        else:
            os.unlink(target)  # enlace duro: el contenido sigue en el origen
        notes.append("enlace quitado")
    if os.path.lexists(target):
        return "conflict", f"{target} sigue ocupado; no se sobrescribe."

    backup = e.get("backup")
    if e.get("moved_to"):
        shutil.move(e["moved_to"], str(target))
        notes.append(f"archivo devuelto desde {e['moved_to']}")
        if backup and os.path.lexists(backup) and Path(backup).parent.parent.name == BACKUP_DIR:
            os.unlink(backup)  # copia de seguridad que creó la propia herramienta
            _tidy_backup_dirs(Path(backup))
        elif backup:
            notes.append(f"el backup {backup} ya no existía")
    elif backup:
        os.rename(backup, target)
        _tidy_backup_dirs(Path(backup))
        notes.append(f"archivo restaurado desde {backup}")
    elif e.get("previous_link"):
        _restore_link(target, e["previous_link"])
        notes.append("enlace anterior recreado")

    _rmdir_if_empty(e.get("created_dirs", []))
    text = "; ".join(notes) or "Sin cambios."
    return "ok", text[0].upper() + text[1:] + "."
