# Harness Skills Linker

Herramienta web local que detecta los harnesses de agentes instalados en tu equipo (Claude Code, Codex, Cursor, OpenCode, Gemini CLI) y enlaza sus skills con symlinks, para mantener una sola copia de cada skill en lugar de una por harness.

## Qué problema resuelve

Cada harness lee sus skills de una carpeta distinta (`~/.claude/skills`, `~/.cursor/skills`, `~/.config/opencode/skills`, ...). Si usas varios, acabas con la misma skill copiada en varios sitios y con versiones que divergen. Esta herramienta elige una carpeta canónica y deja el resto de harnesses apuntando a ella.

- Corre solo en tu máquina: el servidor escucha en `127.0.0.1` y exige un token de sesión en cada llamada a la API.
- No destructivo: nunca borra ni sobrescribe una carpeta real con contenido. Antes de sustituir una carpeta real se hace backup en `.linker-backup`.
- Siempre muestra un plan antes de tocar el disco.

## Requisitos

- Python 3.10 o superior, sin dependencias externas.
- Windows: Modo Desarrollador activado, o una terminal con privilegios, para poder crear symlinks.

## Uso

```bash
python app.py
```

Se abre el navegador en `http://127.0.0.1:<puerto>/?t=<token>`. Si no se abre, usa la URL que imprime la terminal: el token es necesario.

Opciones:

| Opción | Efecto |
| --- | --- |
| `--port N` | Puerto fijo. Por defecto se elige uno libre. |
| `--no-browser` | No abre el navegador. |

### Flujo

1. **Harnesses.** La página lista los harnesses detectados y las skills de cada uno. Puedes añadir una carpeta personalizada con su ruta absoluta.
2. **Alcance.** Elige global de usuario o un directorio de proyecto. Para un proyecto, pulsa **Elegir carpeta** y selecciónala en el diálogo del sistema, o escribe la ruta absoluta. Si el equipo no puede abrir el diálogo (por ejemplo, Linux sin tkinter), escribe la ruta.
3. **Selección.** Marca los harnesses que quieres conectar y la carpeta canónica, que es la que conserva las skills reales.
4. **Plan.** Pulsa **Calcular plan** para ver, skill por skill, qué pasaría:

   | Acción | Significado |
   | --- | --- |
   | Crear enlace | Se creará un symlink hacia la carpeta canónica. |
   | Ya enlazado | El enlace existe y apunta al sitio correcto. |
   | Conflicto | Hay una carpeta real en más de un sitio y la regla elegida no lo resuelve. |
   | Omitido | Se deja como está: conflicto pendiente, enlace roto, sin origen válido o riesgo de bucle. |

5. **Aplicar.** **Aplicar enlaces** ejecuta el plan y muestra el resultado de cada acción.

### Reglas de conflicto

Cuando la misma skill existe como carpeta real en varios harnesses:

| Regla | Efecto |
| --- | --- |
| Sin regla | Marca el conflicto y no toca nada. |
| Preferir la carpeta canónica | La versión de la carpeta canónica gana. Las demás se respaldan y se sustituyen por enlaces. |
| Preferir un harness | La versión de ese harness gana. |
| Mover a la carpeta canónica | Las carpetas reales se mueven a la canónica con backup y se enlazan. Pide confirmación. |

## Harnesses del catálogo

Definidos en [backend/catalog.json](backend/catalog.json), con la fuente oficial de cada ruta.

| Harness | Global | Proyecto |
| --- | --- | --- |
| Agents (carpeta compartida) | `~/.agents/skills` | `.agents/skills` |
| Claude Code | `~/.claude/skills` (o `$CLAUDE_CONFIG_DIR/skills`) | `.claude/skills` |
| Codex | `~/.agents/skills` | `.agents/skills` |
| Cursor | `~/.cursor/skills` | `.cursor/skills` |
| OpenCode | `~/.config/opencode/skills` | `.opencode/skills` |
| Gemini CLI | `~/.gemini/skills` | `.gemini/skills` |

Codex y la carpeta compartida usan la misma ruta. Para añadir un harness, agrega una entrada al catálogo.

## Detener el servidor

- `Ctrl+C` en la terminal.
- El botón **Detener servidor** de la web, que llama a `POST /api/shutdown`.

## API local

Todas las rutas exigen la cabecera de token de sesión que la página recibe al cargarse.

| Ruta | Función |
| --- | --- |
| `GET /api/harnesses` | Escanea el alcance global. |
| `POST /api/scan` | Escanea con alcance, proyecto y carpetas personalizadas. |
| `POST /api/plan` | Calcula el plan sin modificar el disco. |
| `POST /api/apply` | Recalcula el plan y lo ejecuta. |
| `POST /api/pick-folder` | Abre el selector de carpetas del sistema y devuelve `{"path": ...}`, o `null` si se cancela. |
| `POST /api/shutdown` | Detiene el servidor. |

## Estructura

```text
app.py            Punto de entrada
backend/
  catalog.json    Catálogo de harnesses y rutas
  catalog.py      Carga del catálogo y expansión de rutas
  scanner.py      Detección de harnesses y lectura de skills
  linker.py       Plan (dry-run) y aplicación de enlaces
  symlinks.py     Creación de symlinks por sistema operativo
  picker.py       Selector nativo de carpeta (tkinter en subproceso)
  server.py       Servidor HTTP local y API JSON
frontend/         HTML, CSS y JS sin framework
tests/            Tests unitarios, de API y end-to-end
```

## Tests

```bash
python -m unittest discover -s tests -t . -v
```

CI ejecuta la suite en Ubuntu, macOS y Windows con Python 3.10 y 3.13.
