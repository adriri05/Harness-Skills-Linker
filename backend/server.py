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
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import catalog

HOST = "127.0.0.1"
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
TOKEN_HEADER = "X-Session-Token"
TOKEN_META_PLACEHOLDER = "__SESSION_TOKEN__"


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
        if not self._guard():
            return
        url = urlsplit(self.path)
        if not url.path.startswith("/api/"):
            self._error(HTTPStatus.METHOD_NOT_ALLOWED, "Método no permitido")
            return
        self._api(url.path)

    def _api(self, path: str) -> None:
        if not self._token_ok(self.headers.get(TOKEN_HEADER)):
            self._error(HTTPStatus.UNAUTHORIZED, "Token de sesión ausente o inválido")
            return
        if self.command == "GET" and path == "/api/harnesses":
            self._json(
                {
                    "harnesses": [
                        {
                            "id": h.id,
                            "name": h.name,
                            "global_path": str(h.global_path()),
                            "project_template": h.project_template,
                            "verified": h.verified,
                            "note": h.note,
                        }
                        for h in catalog.load_catalog()
                    ]
                }
            )
            return
        self._error(HTTPStatus.NOT_FOUND, "Ruta de API desconocida")

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
        pass
    finally:
        server.server_close()
