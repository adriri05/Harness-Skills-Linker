"""Servidor HTTP local y API JSON.

Defensas, porque la API acabará creando symlinks:
- Escucha solo en 127.0.0.1.
- Valida la cabecera Host (anti DNS rebinding) y Origin (anti CSRF).
- La API exige un token de sesión aleatorio en la cabecera X-Session-Token.
  El token solo llega al navegador por la URL de arranque (?t=...), que
  index.html consume desde una etiqueta <meta>.
"""

from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import catalog, history, instructions, linker, picker, scanner

HOST = "127.0.0.1"
MAX_BODY = 1024 * 1024
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
TOKEN_HEADER = "X-Session-Token"
TOKEN_META_PLACEHOLDER = "__SESSION_TOKEN__"


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _bad(message: str) -> ApiError:
    return ApiError(HTTPStatus.BAD_REQUEST, message)


def _check_keys(body: dict, allowed: set[str], required: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise _bad(f"Claves no admitidas: {', '.join(unknown)}. Quítalas del cuerpo.")
    missing = sorted(required - set(body))
    if missing:
        raise _bad(f"Faltan claves obligatorias: {', '.join(missing)}.")


def _validate_scope(body: dict) -> tuple[str, str | None]:
    scope = body.get("scope")
    if scope not in ("global", "project"):
        raise _bad("scope debe ser 'global' o 'project'.")
    project_dir = body.get("project_dir")
    if scope == "project":
        if not isinstance(project_dir, str) or not project_dir:
            raise _bad("El alcance 'project' necesita project_dir con una ruta absoluta.")
        if not os.path.isabs(project_dir):
            raise _bad("project_dir debe ser una ruta absoluta.")
        if not os.path.isdir(project_dir):
            raise _bad("project_dir no existe o no es una carpeta. Elige una carpeta existente.")
    elif project_dir is not None and not isinstance(project_dir, str):
        raise _bad("project_dir debe ser texto o null.")
    return scope, project_dir


def _validate_custom(raw) -> list[dict] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise _bad("custom debe ser una lista o null.")
    result = []
    for item in raw:
        if not isinstance(item, dict):
            raise _bad("Cada entrada de custom debe ser un objeto {id, name, path}.")
        _check_keys(item, {"id", "name", "path"}, {"id", "name", "path"})
        for key in ("id", "name", "path"):
            if not isinstance(item[key], str) or not item[key].strip():
                raise _bad(f"custom.{key} debe ser un texto no vacío.")
        if not os.path.isabs(item["path"]):
            raise _bad(f"custom.path debe ser una ruta absoluta: {item['path']}")
        result.append({k: item[k] for k in ("id", "name", "path")})
    return result


def _custom_harnesses(custom: list[dict] | None) -> list[catalog.Harness]:
    return [
        catalog.Harness(
            id=c["id"],
            name=c["name"],
            global_template=c["path"],
            project_template="",
            binaries=(),
            verified=False,
        )
        for c in custom or []
    ]


def validate_scan(body: dict) -> dict:
    _check_keys(body, {"scope", "project_dir", "custom"}, {"scope"})
    scope, project_dir = _validate_scope(body)
    return {"scope": scope, "project_dir": project_dir, "custom": _validate_custom(body.get("custom"))}


def validate_plan(body: dict) -> dict:
    _check_keys(
        body,
        {"harness_ids", "scope", "project_dir", "canonical", "conflict_rule", "custom"},
        {"harness_ids", "scope", "conflict_rule"},
    )
    ids = body["harness_ids"]
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise _bad("harness_ids debe ser una lista de textos.")
    scope, project_dir = _validate_scope(body)
    canonical = body.get("canonical")
    if canonical is not None and not isinstance(canonical, str):
        raise _bad("canonical debe ser texto o null.")
    rule = body["conflict_rule"]
    if not isinstance(rule, str) or not rule:
        raise _bad("conflict_rule debe ser un texto no vacío.")
    return {
        "harness_ids": ids,
        "scope": scope,
        "project_dir": project_dir,
        "canonical": canonical,
        "conflict_rule": rule,
        "custom": _validate_custom(body.get("custom")),
    }


def validate_instructions_scan(body: dict) -> dict:
    _check_keys(body, {"scope", "project_dir"}, {"scope"})
    scope, project_dir = _validate_scope(body)
    return {"scope": scope, "project_dir": project_dir}


def validate_instructions_plan(body: dict) -> dict:
    _check_keys(
        body,
        {"harness_ids", "scope", "project_dir", "canonical", "conflict_rule"},
        {"harness_ids", "scope", "conflict_rule"},
    )
    kwargs = validate_plan(body)
    del kwargs["custom"]
    return kwargs


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, token: str, frontend_dir: Path = FRONTEND_DIR):
        super().__init__((HOST, port), Handler)
        self.token = token
        self.frontend_dir = frontend_dir.resolve()

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def allowed_hosts(self) -> set[str]:
        return {f"{HOST}:{self.port}", f"localhost:{self.port}"}

    @property
    def allowed_origins(self) -> set[str]:
        return {f"http://{h}" for h in self.allowed_hosts}

    def stop(self) -> None:
        """Detiene serve_forever y cierra el socket. Llamar desde un hilo distinto al del bucle."""
        self.shutdown()
        self.server_close()

    def start_url(self) -> str:
        return f"http://{HOST}:{self.port}/?t={self.token}"


class Handler(BaseHTTPRequestHandler):
    server: LocalServer
    server_version = "HarnessSkillsLinker"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    # --- validación común ---

    def _guard(self) -> bool:
        """Devuelve True si la petición pasa Host y Origin; si no, responde 403."""
        if self.headers.get("Host") not in self.server.allowed_hosts:
            self._error(HTTPStatus.FORBIDDEN, "Host no permitido")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.server.allowed_origins:
            self._error(HTTPStatus.FORBIDDEN, "Origin no permitido")
            return False
        return True

    def _token_ok(self, candidate: str | None) -> bool:
        return candidate is not None and hmac.compare_digest(
            candidate.encode(), self.server.token.encode()
        )

    # --- respuestas ---

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json({"error": message}, status)

    # --- rutas ---

    def do_GET(self) -> None:
        if not self._guard():
            return
        url = urlsplit(self.path)
        if url.path.startswith("/api/"):
            self._api(url.path)
        else:
            self._static(url.path, parse_qs(url.query))

    do_HEAD = do_GET

    def do_POST(self) -> None:
        self._body_read = False
        try:
            if not self._guard():
                return
            url = urlsplit(self.path)
            if not url.path.startswith("/api/"):
                self._error(HTTPStatus.METHOD_NOT_ALLOWED, "Método no permitido")
                return
            self._api(url.path)
        finally:
            if not self._body_read:
                self._discard_body()

    def _discard_body(self) -> None:
        """Lee el cuerpo que no se usó antes de cerrar.

        Si el cliente aún envía el cuerpo cuando el servidor ya respondió y cerró, Windows
        resetea la conexión y el cliente pierde la respuesta (WinError 10053).
        Un cuerpo declarado por encima de MAX_BODY no se lee: se rechaza por la cabecera.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if 0 < length <= MAX_BODY:
            try:
                self.rfile.read(length)
            except OSError:
                pass

    def _api(self, path: str) -> None:
        if not self._token_ok(self.headers.get(TOKEN_HEADER)):
            self._error(HTTPStatus.UNAUTHORIZED, "Token de sesión ausente o inválido")
            return
        try:
            if self.command == "GET" and path == "/api/harnesses":
                self._json(self._scan({"scope": "global"}))
            elif self.command == "POST" and path == "/api/scan":
                self._json(self._scan(self._read_json()))
            elif self.command == "POST" and path in ("/api/plan", "/api/apply"):
                self._json(self._linker_call(path.rsplit("/", 1)[1], self._read_json()))
            elif self.command == "POST" and path == "/api/instructions/scan":
                args = validate_instructions_scan(self._read_json())
                self._json({"harnesses": instructions.scan(args["scope"], args["project_dir"])})
            elif self.command == "POST" and path in ("/api/instructions/plan", "/api/instructions/apply"):
                self._json(self._instructions_call(path.rsplit("/", 1)[1], self._read_json()))
            elif self.command == "GET" and path == "/api/history":
                self._json({"last": history.summarize(history.last())})
            elif self.command == "POST" and path in ("/api/undo-plan", "/api/undo"):
                self._json(self._undo_call(path == "/api/undo", self._read_json()))
            elif self.command == "POST" and path == "/api/pick-folder":
                self._json(self._pick_folder())
            elif self.command == "POST" and path == "/api/shutdown":
                self._shutdown()
            else:
                raise ApiError(HTTPStatus.NOT_FOUND, "Ruta de API desconocida")
        except ApiError as exc:
            self._error(exc.status, exc.message)
        except Exception:  # noqa: BLE001 - la API nunca devuelve una traza
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "Error interno del servidor. Revisa la terminal donde corre y vuelve a intentarlo.",
            )

    def _read_json(self) -> dict:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "Envía el cuerpo con Content-Type: application/json.",
            )
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            raise _bad("Falta Content-Length con el tamaño del cuerpo.") from None
        if length < 0 or length > MAX_BODY:
            raise _bad("El cuerpo supera 1 MB. Envía menos datos.")
        self._body_read = True
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _bad("El cuerpo no es JSON válido.") from None
        if not isinstance(body, dict):
            raise _bad("El cuerpo debe ser un objeto JSON.")
        return body

    def _scan(self, body: dict) -> dict:
        args = validate_scan(body)
        harnesses = catalog.load_catalog() + _custom_harnesses(args["custom"])
        scans = scanner.scan_all(args["scope"], args["project_dir"], harnesses)
        return {"harnesses": [s.to_dict() for s in scans]}

    def _linker_call(self, name: str, body: dict) -> dict:
        kwargs = validate_plan(body)
        try:
            from . import linker
        except ImportError:
            linker = None
        func = getattr(linker, "build_plan" if name == "plan" else "apply", None)
        if func is None:
            raise ApiError(
                HTTPStatus.NOT_IMPLEMENTED,
                f"La operación '{name}' no está disponible en esta versión. Actualiza la aplicación.",
            )
        try:
            return func(**kwargs).to_dict()
        except (ValueError, KeyError) as exc:
            raise _bad(f"No se pudo completar '{name}': {exc}") from None

    def _instructions_call(self, name: str, body: dict) -> dict:
        kwargs = validate_instructions_plan(body)
        func = instructions.build_plan if name == "plan" else instructions.apply
        try:
            return func(**kwargs).to_dict()
        except (ValueError, KeyError) as exc:
            raise _bad(f"No se pudo completar '{name}': {exc}") from None

    def _undo_call(self, execute: bool, body: dict) -> dict:
        _check_keys(body, {"id"}, set())
        app_id = body.get("id")
        if app_id is not None and not isinstance(app_id, str):
            raise _bad("id debe ser el texto de la aplicación o null.")
        try:
            if execute:
                return linker.undo(app_id).to_dict()
            return linker.undo_plan(app_id).to_dict()
        except linker.UndoError as exc:
            status = HTTPStatus.NOT_FOUND if exc.code == "none" else HTTPStatus.CONFLICT
            raise ApiError(status, str(exc)) from None

    def _pick_folder(self) -> dict:
        try:
            return {"path": picker.pick_folder("Carpeta del proyecto")}
        except picker.PickerUnavailable:
            raise ApiError(
                HTTPStatus.NOT_IMPLEMENTED,
                "Este equipo no puede abrir el selector de carpetas. Escribe la ruta a mano.",
            ) from None

    def _shutdown(self) -> None:
        self._json({"ok": True})
        self.wfile.flush()
        threading.Thread(target=self.server.stop, name="shutdown", daemon=True).start()

    def _static(self, path: str, query: dict[str, list[str]]) -> None:
        if self.command == "POST":
            self._error(HTTPStatus.METHOD_NOT_ALLOWED, "Método no permitido")
            return
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (self.server.frontend_dir / relative).resolve()
        if not target.is_relative_to(self.server.frontend_dir) or not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, "No encontrado")
            return
        data = target.read_bytes()
        if target.name == "index.html":
            token = (query.get("t") or [None])[0]
            if not self._token_ok(token):
                self._error(
                    HTTPStatus.FORBIDDEN,
                    "Abre la URL que imprimió el servidor al arrancar, incluye el token de sesión",
                )
                return
            data = data.replace(TOKEN_META_PLACEHOLDER.encode(), self.server.token.encode())
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
            "application/javascript",
            "application/json",
        ):
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, data, content_type)


def create_server(port: int = 0, frontend_dir: Path = FRONTEND_DIR) -> LocalServer:
    """Crea el servidor sin arrancarlo. port=0 elige un puerto libre."""
    return LocalServer(port, secrets.token_urlsafe(32), frontend_dir)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Harness Skills Linker")
    parser.add_argument("--port", type=int, default=0, help="Puerto; 0 elige uno libre")
    parser.add_argument("--no-browser", action="store_true", help="No abrir el navegador")
    args = parser.parse_args(argv)

    server = create_server(args.port)
    url = server.start_url()
    print(f"Servidor en {url}\nCtrl+C para detenerlo.", flush=True)
    if not args.no_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.", flush=True)
    finally:
        server.server_close()
