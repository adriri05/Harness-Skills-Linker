"""Plan dry-run (issue #5), aplicación de enlaces (issues #6, #7, #8) y deshacer (issue #32).

Los archivos de instrucciones (issue #33) viven en `instructions`; el deshacer es común.

`build_plan` solo lee el disco. `apply` recalcula el plan, lo ejecuta y registra
lo hecho en `history`. `undo_plan` y `undo` revierten la última aplicación.
Nunca se borra ni se sobrescribe una carpeta real: solo se sustituyen enlaces,
y con `move_to_canonical` las carpetas reales se mueven tras hacer backup.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import history
from .catalog import Harness, load_catalog
from .scanner import Skill, scan_skill_dir
from .symlinks import LinkError, can_create_symlinks, is_link, make_dir_link, remove_link

ConflictRule = str  # "none" | "prefer_canonical" | "prefer_harness:<id>" | "move_to_canonical" | "move_all_to_canonical"

BACKUP_DIR = ".linker-backup"
ACTIONS = ("create_link", "already_linked", "conflict", "skipped")


@dataclass
class PlanAction:
    skill: str
    harness_id: str
    target: str
    source: Optional[str]
    action: str
    reason: str
    resolution: Optional[str] = None
    # Campos internos para apply; no se serializan.
    replace_link: bool = False
    backup: bool = False
    move_to: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "skill": self.skill,
            "harness_id": self.harness_id,
            "target": self.target,
            "source": self.source,
            "action": self.action,
            "reason": self.reason,
            "resolution": self.resolution,
        }


@dataclass
class Plan:
    actions: list[PlanAction] = field(default_factory=list)
    canonical: Optional[str] = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        counts = {a: 0 for a in ACTIONS}
        for a in self.actions:
            counts[a.action] += 1
        return counts

    def to_dict(self) -> dict:
        return {
            "actions": [a.to_dict() for a in self.actions],
            "summary": self.summary(),
            "canonical": self.canonical,
            "warnings": list(self.warnings),
        }


@dataclass
class ApplyResult:
    results: list[dict] = field(default_factory=list)
    application_id: Optional[str] = None  # None si no cambió nada en el disco

    def to_dict(self) -> dict:
        summary = {"ok": 0, "error": 0, "skipped": 0}
        for r in self.results:
            summary[r["status"]] += 1
        return {"results": list(self.results), "summary": summary,
                "application_id": self.application_id}


@dataclass
class _Canon:
    id: str
    dir: Path
    selected: bool


# ---------------------------------------------------------------- utilidades

def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _inside(path: str, parent: str) -> bool:
    p, q = _norm(path), _norm(parent)
    return p == q or p.startswith(q.rstrip(os.sep) + os.sep)


def _harness_dir(h: Harness, scope: str, project_dir: str | None) -> Path:
    if scope == "project":
        if not project_dir:
            raise ValueError("El alcance 'project' necesita project_dir")
        return h.project_path(project_dir)
    if scope != "global":
        raise ValueError(f"Alcance desconocido: {scope}")
    return h.global_path()


def _custom_harnesses(custom: list[dict] | None) -> list[Harness]:
    out = []
    for item in custom or []:
        try:
            hid, name, path = item["id"], item.get("name") or item["id"], item["path"]
        except (KeyError, TypeError, AttributeError):
            raise ValueError("Cada carpeta personalizada necesita 'id' y 'path'") from None
        if not path:
            raise ValueError(f"La carpeta personalizada '{hid}' no tiene ruta")
        out.append(Harness(id=hid, name=name, global_template=path, project_template="",
                           binaries=(), verified=False))
    return out


def _validate_rule(rule: str) -> None:
    if rule in ("none", "prefer_canonical", "move_to_canonical", "move_all_to_canonical"):
        return
    if rule.startswith("prefer_harness:") and rule.split(":", 1)[1]:
        return
    raise ValueError(f"Regla de conflicto desconocida: {rule}")


def _broken_reason(path: str) -> str:
    """Distingue un bucle de enlaces de un destino inexistente."""
    seen: set[str] = set()
    current = path
    for _ in range(64):
        key = os.path.normcase(os.path.abspath(current))
        if key in seen:
            return "bucle de enlaces"
        seen.add(key)
        if not is_link(current):
            break
        try:
            nxt = os.readlink(current)
        except OSError:
            break
        current = os.path.join(os.path.dirname(current), nxt)
    else:
        return "bucle de enlaces"
    return "enlace roto, el destino no existe"


# --------------------------------------------------------------------- plan

def build_plan(
    harness_ids: list[str],
    scope: str = "global",
    project_dir: str | None = None,
    canonical: str | None = None,
    conflict_rule: ConflictRule = "none",
    custom: list[dict] | None = None,
) -> Plan:
    _validate_rule(conflict_rule)
    plan = Plan()

    available = {h.id: h for h in load_catalog()}
    for h in _custom_harnesses(custom):
        available[h.id] = h  # una carpeta personalizada con el mismo id sustituye a la del catálogo

    participants: list[tuple[Harness, Path]] = []
    seen_ids: set[str] = set()
    for hid in harness_ids:
        if hid in seen_ids:
            continue
        seen_ids.add(hid)
        if hid not in available:
            raise ValueError(f"Harness desconocido: {hid}")
        participants.append((available[hid], _harness_dir(available[hid], scope, project_dir)))
    dirs = {h.id: d for h, d in participants}

    canon: Optional[_Canon] = None
    if canonical:
        if canonical in available:
            cdir = dirs.get(canonical) or _harness_dir(available[canonical], scope, project_dir)
            canon = _Canon(canonical, cdir, canonical in dirs)
        else:
            expanded = Path(canonical).expanduser()
            if not expanded.is_absolute():
                raise ValueError(
                    f"Canonical debe ser un id de harness o una ruta absoluta: {canonical}"
                )
            canon = _Canon("canonical", expanded, False)
        plan.canonical = str(canon.dir)

    if conflict_rule in ("prefer_canonical", "move_to_canonical", "move_all_to_canonical") and not canon:
        plan.warnings.append(f"La regla {conflict_rule} necesita una carpeta canónica. Sin ella, los conflictos quedan sin resolver.")
    if conflict_rule.startswith("prefer_harness:"):
        pid = conflict_rule.split(":", 1)[1]
        if pid not in dirs:
            plan.warnings.append(f"La regla prefiere '{pid}', que no está entre los harnesses seleccionados.")

    if os.name == "nt" and not can_create_symlinks():
        plan.warnings.append(
            "Sin privilegios, Windows no permite symlinks. Se crearán junctions, que no necesitan "
            "permisos y funcionan igual para estas carpetas."
        )

    # Lectura del disco: skills por harness, indexadas por nombre.
    by_harness: dict[str, dict[str, Skill]] = {}
    for h, d in participants:
        by_harness[h.id] = {s.name: s for s in scan_skill_dir(d)}
    if canon and not canon.selected:
        by_harness[canon.id] = {s.name: s for s in scan_skill_dir(canon.dir)}

    names = sorted({n for skills in by_harness.values() for n in skills}, key=str.lower)
    order = [h.id for h, _ in participants]
    for name in names:
        ents = {hid: by_harness[hid][name] for hid in by_harness if name in by_harness[hid]}
        plan.actions.extend(_plan_skill(name, ents, order, dirs, canon, conflict_rule))
    return plan


def _plan_skill(
    name: str,
    ents: dict[str, Skill],
    order: list[str],
    dirs: dict[str, Path],
    canon: Optional[_Canon],
    rule: str,
) -> list[PlanAction]:
    good = {hid: s for hid, s in ents.items() if s.kind != "broken"}
    cands: dict[str, list[str]] = {}
    for hid, s in good.items():
        cands.setdefault(_norm(s.real_path), []).append(hid)
    multiple = len(cands) > 1
    canon_skill = good.get(canon.id) if canon else None

    src: Optional[str] = None
    winner: Optional[str] = None  # harness cuya carpeta real se mueve a canonical
    if canon_skill:
        src = canon_skill.real_path
    elif len(cands) == 1:
        src = next(iter(good.values())).real_path
    if multiple and rule.startswith("prefer_harness:"):
        pref = good.get(rule.split(":", 1)[1])
        if pref:
            src = pref.real_path
    elif canon and not canon_skill and (
        rule == "move_all_to_canonical" or (multiple and rule == "move_to_canonical")
    ):
        # move_all_to_canonical también mueve las skills que solo existen en un harness.
        winner = next((h for h in order if h in good and good[h].kind == "real"), None)
        if winner:
            src = str(canon.dir / name)
    conflict = multiple and src is None
    resolution = rule if multiple and rule != "none" else None

    rows: list[PlanAction] = []

    def row(hid, action, reason, source=None, res=resolution, **flags):
        rows.append(PlanAction(name, hid, str(dirs[hid] / name), source, action, reason, res, **flags))

    for hid in order:
        s = ents.get(hid)
        target_dir = str(dirs[hid])
        if conflict:
            if s is None:
                row(hid, "skipped", "Pendiente de resolver el conflicto.")
            elif s.kind == "broken":
                row(hid, "skipped", f"{_broken_reason(s.path).capitalize()}; pendiente del conflicto.")
            else:
                others = ", ".join(h for h in cands[_norm(s.real_path)] if h != hid)
                row(hid, "conflict", "Existe en varios harnesses con contenido distinto"
                    + (f" (comparte destino con {others})." if others else "."))
            continue
        if src is None:
            why = _broken_reason(s.path).capitalize() if s else "Sin origen válido"
            row(hid, "skipped", f"{why} y no hay origen válido para enlazar.")
            continue
        if winner and s is None and canon and hid == canon.id:
            row(hid, "skipped", f"Recibe la carpeta movida desde {winner}.")
            continue
        if _inside(target_dir, src):
            row(hid, "skipped", "La carpeta del harness está dentro del origen y crearía un bucle.")
            continue
        if s is None:
            row(hid, "create_link", "Falta en este harness.", src)
        elif s.kind == "broken":
            row(hid, "create_link", f"Sustituye un enlace: {_broken_reason(s.path)}.", src,
                replace_link=True)
        elif hid == winner:
            row(hid, "create_link", f"Mueve la carpeta a {src} (con backup) y la enlaza.", src,
                move_to=src)
        elif _norm(s.real_path) == _norm(src):
            row(hid, "already_linked",
                "Carpeta origen." if s.kind == "real" else "Ya enlazada al origen.", src)
        elif s.kind == "symlink":
            row(hid, "create_link", f"Reapunta el enlace desde {s.real_path}.", src, replace_link=True)
        elif rule in ("move_to_canonical", "move_all_to_canonical") and multiple:
            row(hid, "create_link", "Carpeta real duplicada. Se hace backup y se enlaza al origen.",
                src, backup=True)
        elif multiple and rule != "none":
            row(hid, "skipped", f"Se conserva la carpeta real porque la regla {rule} no la sustituye.", src)
        else:
            row(hid, "conflict", "Carpeta real distinta de la fuente canónica.", src, res=None)
    if winner:
        rows.sort(key=lambda r: r.harness_id != winner)
    return rows


# ------------------------------------------------------------------- apply

def apply(
    harness_ids: list[str],
    scope: str = "global",
    project_dir: str | None = None,
    canonical: str | None = None,
    conflict_rule: ConflictRule = "none",
    custom: list[dict] | None = None,
) -> ApplyResult:
    plan = build_plan(harness_ids, scope, project_dir, canonical, conflict_rule, custom)
    result = ApplyResult()
    entries: list[dict] = []
    for a in plan.actions:
        if a.action == "create_link":
            entry = {"skill": a.skill, "harness_id": a.harness_id}
            try:
                status, message = _execute(a, entry)
            except LinkError as exc:
                status, message = "error", str(exc)
            except OSError as exc:
                status, message = "error", f"{type(exc).__name__}: {exc}"
            if entry.get("changed"):  # también un fallo a medias deja algo que revertir
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


def _backup_path(target: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    n = 1
    while True:
        folder = target.parent / BACKUP_DIR / (stamp if n == 1 else f"{stamp}-{n}")
        if not os.path.lexists(folder / target.name):
            break
        n += 1
    folder.mkdir(parents=True, exist_ok=True)
    return folder / target.name


def _missing_dirs(path: Path) -> list[str]:
    """Carpetas que habría que crear para que `path` exista, de la más profunda a la más alta."""
    missing: list[str] = []
    current = path
    while not os.path.lexists(current) and current != current.parent:
        missing.append(str(current))
        current = current.parent
    return missing


def _raw_link(path: Path) -> str:
    raw = os.readlink(path)
    return raw[4:] if raw.startswith("\\\\?\\") else raw  # prefijo \\?\ de las junctions


def _execute(a: PlanAction, entry: dict | None = None) -> tuple[str, str]:
    """Ejecuta una acción. Si se pasa `entry`, anota en él lo necesario para deshacerla."""
    entry = entry if entry is not None else {}
    target, src = Path(a.target), a.source
    notes: list[str] = []
    entry.update(target=a.target, source=src, link_created=False, created_dirs=[])

    if a.move_to:
        if is_link(target) or not target.is_dir():
            return "error", "La carpeta a mover ya no es una carpeta real."
        dest = Path(a.move_to)
        if os.path.lexists(dest):
            return "error", f"{dest} ya existe; no se sobrescribe."
        backup = _backup_path(target)
        shutil.copytree(target, backup, symlinks=True)
        entry.update(backup=str(backup), changed=True)
        entry["created_dirs"] += _missing_dirs(dest.parent)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(dest))
        entry["moved_to"] = str(dest)
        notes.append(f"movida a {dest}, backup en {backup}")
    elif not src or not os.path.isdir(src):
        return "error", f"El origen {src} no existe."

    if os.path.lexists(target):
        if is_link(target):
            if os.path.isdir(target) and _norm(str(target)) == _norm(src):
                return "skipped", "Ya enlazado."
            if not a.replace_link:
                return "error", "Ya hay un enlace distinto. No se sustituye."
            entry["previous_link"] = _raw_link(target)
            remove_link(target)
            entry["changed"] = True
        elif target.is_dir() and a.backup:
            backup = _backup_path(target)
            os.rename(target, backup)
            entry.update(backup=str(backup), changed=True)
            notes.append(f"backup en {backup}")
        else:
            return "error", "Ya hay una carpeta real o un fichero. No se sobrescribe."

    entry["created_dirs"] += _missing_dirs(target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    kind = make_dir_link(target, src)
    entry.update(kind=kind, link_created=True, changed=True)
    notes.append(f"{kind} creado")
    text = "; ".join(notes)
    return "ok", text[0].upper() + text[1:] + "."


# -------------------------------------------------------------------- deshacer

UNDO_ACTIONS = ("remove_link", "restore", "conflict", "skipped")


class UndoError(Exception):
    """No se puede deshacer. `code` es 'none' (sin historial) o 'not_last'."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class UndoAction:
    entry: dict
    action: str
    reason: str

    def to_dict(self) -> dict:
        e = self.entry
        return {"skill": e["skill"], "harness_id": e["harness_id"], "target": e["target"],
                "action": self.action, "reason": self.reason}


@dataclass
class UndoPlan:
    application: dict
    actions: list[UndoAction] = field(default_factory=list)

    def to_dict(self) -> dict:
        counts = {a: 0 for a in UNDO_ACTIONS}
        for a in self.actions:
            counts[a.action] += 1
        return {"application": history.summarize(self.application),
                "actions": [a.to_dict() for a in self.actions],
                "summary": counts, "warnings": []}


@dataclass
class UndoResult:
    application_id: str
    results: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        summary = {"ok": 0, "error": 0, "skipped": 0, "conflict": 0}
        for r in self.results:
            summary[r["status"]] += 1
        return {"application_id": self.application_id, "results": list(self.results),
                "summary": summary}


def _is_empty_dir(path: Path) -> bool:
    return not is_link(path) and path.is_dir() and not any(path.iterdir())


def _restores(e: dict) -> bool:
    return bool(e.get("backup") or e.get("moved_to") or e.get("previous_link"))


def _inspect_undo(e: dict) -> UndoAction:
    """Decide qué haría deshacer esta entrada, sin tocar el disco."""
    if e.get("item") == "file":
        from . import instructions  # import tardío: instructions importa este módulo

        return instructions.inspect_undo(e)
    target = Path(e["target"])
    backup = e.get("backup")
    where = f" El backup sigue en {backup}." if backup and os.path.lexists(backup) else ""

    if e.get("link_created"):
        if not os.path.lexists(target):
            return UndoAction(e, "skipped", "El enlace ya no existe; no se toca." + where)
        if is_link(target):
            if _norm(str(target)) != _norm(e["source"]):
                return UndoAction(e, "skipped",
                                  f"El enlace apunta ahora a {_norm(str(target))}; no se toca." + where)
        elif not _restores(e):
            return UndoAction(e, "skipped", "Ya no es un enlace; no se toca.")
        elif not _is_empty_dir(target):
            return UndoAction(e, "conflict",
                              "Ya existe una carpeta o un fichero con contenido en el destino; no se sobrescribe." + where)
    elif os.path.lexists(target) and not _is_empty_dir(target):
        return UndoAction(e, "conflict",
                          "El destino ya existe y tiene contenido; no se sobrescribe." + where)

    if e.get("moved_to"):
        dest = Path(e["moved_to"])
        if is_link(dest) or not dest.is_dir():
            return UndoAction(e, "skipped",
                              f"La carpeta movida ya no está en {dest}; no se toca." + where)
        return UndoAction(e, "restore", f"Quita el enlace y devuelve la carpeta desde {dest}.")
    if backup:
        if not os.path.lexists(backup):
            return UndoAction(e, "skipped",
                              f"El backup {backup} ya no existe y no se puede restaurar. El enlace se deja como está.")
        return UndoAction(e, "restore", f"Quita el enlace y restaura la carpeta desde {backup}.")
    if e.get("previous_link"):
        return UndoAction(e, "restore", f"Quita el enlace y recrea el anterior hacia {e['previous_link']}.")
    return UndoAction(e, "remove_link", "Quita el enlace creado.")


def _tidy_backup_dirs(backup: Path) -> None:
    for folder in (backup.parent, backup.parent.parent):
        if folder.name != BACKUP_DIR and folder.parent.name != BACKUP_DIR:
            return
        try:
            folder.rmdir()
        except OSError:
            return


def _rmdir_if_empty(paths: list[str]) -> None:
    for p in paths:
        try:
            os.rmdir(p)
        except OSError:
            pass


def _restore_link(target: Path, raw: str) -> None:
    dest = raw if os.path.isabs(raw) else os.path.join(target.parent, raw)
    try:
        os.symlink(raw, target, target_is_directory=True)
    except (OSError, NotImplementedError):
        make_dir_link(target, dest)


def _run_undo(ua: UndoAction) -> tuple[str, str]:
    e = ua.entry
    if e.get("item") == "file":
        from . import instructions

        return instructions.run_undo(ua)
    if ua.action in ("skipped", "conflict"):
        return ua.action, ua.reason
    target = Path(e["target"])
    notes: list[str] = []

    if e.get("link_created") and is_link(target):
        remove_link(target)
        notes.append("enlace quitado")
    elif _is_empty_dir(target):
        os.rmdir(target)
    if os.path.lexists(target):
        return "conflict", f"{target} sigue ocupado; no se sobrescribe."

    backup = e.get("backup")
    if e.get("moved_to"):
        shutil.move(e["moved_to"], str(target))
        notes.append(f"carpeta devuelta desde {e['moved_to']}")
        if backup and os.path.lexists(backup) and Path(backup).parent.parent.name == BACKUP_DIR:
            shutil.rmtree(backup)  # copia de seguridad que creó la propia herramienta
            _tidy_backup_dirs(Path(backup))
        elif backup:
            notes.append(f"el backup {backup} ya no existía")
    elif backup:
        os.rename(backup, target)
        _tidy_backup_dirs(Path(backup))
        notes.append(f"carpeta restaurada desde {backup}")
    elif e.get("previous_link"):
        _restore_link(target, e["previous_link"])
        notes.append("enlace anterior recreado")

    _rmdir_if_empty(e.get("created_dirs", []))
    text = "; ".join(notes) or "Sin cambios."
    return "ok", text[0].upper() + text[1:] + "."


def _application_to_undo(application_id: str | None) -> dict:
    app = history.last()
    if app is None:
        raise UndoError("none", "No hay ninguna aplicación que deshacer.")
    if application_id is not None and application_id != app["id"]:
        raise UndoError("not_last", "Solo se puede deshacer la última aplicación. Vuelve a cargar el plan.")
    return app


def undo_plan(application_id: str | None = None) -> UndoPlan:
    """Qué haría deshacer la última aplicación. Solo lee el disco."""
    app = _application_to_undo(application_id)
    plan = UndoPlan(app)
    plan.actions = [_inspect_undo(e) for e in reversed(app["entries"])]
    return plan


def undo(application_id: str | None = None) -> UndoResult:
    """Deshace la última aplicación. Lo que no se puede revertir se deja y se avisa."""
    app = _application_to_undo(application_id)
    result = UndoResult(app["id"])
    pending: list[dict] = []
    for e in reversed(app["entries"]):
        ua = _inspect_undo(e)
        try:
            status, message = _run_undo(ua)
        except (OSError, shutil.Error) as exc:
            status, message = "error", f"{type(exc).__name__}: {exc}"
        if status in ("conflict", "error"):
            pending.append(e)  # sigue registrada para reintentar tras resolverlo a mano
        result.results.append({**ua.to_dict(), "status": status, "message": message})
    history.replace_last(app["id"], list(reversed(pending)))
    return result
