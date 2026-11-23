# Harness Skills Linker (Web Local Interactiva)

Herramienta web ejecutada en local para detectar los *harnesses* de agentes/entornos instalados en el equipo, inspeccionar las *skills* existentes y vincularlas de forma interactiva entre los *harnesses* seleccionados (mediante enlaces simbólicos / *symlinks*) sin duplicar carpetas ni perder trabajo.

## 1. Objetivo y enfoque

- **Uso local y seguro:** la interfaz corre en el navegador apuntando a un servidor local en `127.0.0.1` (puerto configurable o automático libre), evitando exponer rutas o acciones del sistema fuera de la máquina.
- **Interactividad real:** cualquier persona puede ver qué *harnesses* se detectan, elegir cuáles conectar entre sí, previsualizar cambios (*dry-run*) y aplicarlos con un clic.
- **No destructivo:** nunca borra carpetas reales con contenido sin aviso previo y evita bucles o cadenas rotas de enlaces simbólicos.

## 2. Arquitectura propuesta

Una página web pura no tiene acceso directo al sistema de archivos ni permisos para crear *symlinks*, por lo que el proyecto se divide en dos partes ligeras:

1. **Backend local (Python estándar `http.server`):**
   - Escanea rutas y binarios conocidos para detectar *harnesses* instalados.
   - Lista las *skills* encontradas en cada carpeta (`SKILL.md` o subcarpetas válidas) y resuelve sus rutas reales.
   - Calcula el plan de enlaces (*preview*) y detecta conflictos o duplicados.
   - Ejecuta la creación/actualización de *symlinks* cuando el usuario confirma.
2. **Frontend web local (HTML + CSS + JS vanilla):**
   - Muestra tarjetas o tabla de *harnesses* detectados (y opción para añadir una carpeta personalizada).
   - Permite activar/desactivar qué *harnesses* participan en la sincronización y elegir alcance (global en usuario o directorio de proyecto).
   - Muestra una vista previa clara de acciones (`crear enlace`, `ya enlazado`, `conflicto`, `omitido`) antes de aplicar.

## 3. Flujo de usuario

1. **Arranque:** el usuario ejecuta un comando local (`python app.py`) y se abre automáticamente su navegador en `http://127.0.0.1:<puerto>`.
2. **Detección automática:** la app muestra los *harnesses* encontrados en el sistema (por ejemplo: `~/.agents/skills`, Claude Code, Cursor, Codex, OpenCode, etc.) y las *skills* detectadas en cada uno.
3. **Selección:** el usuario marca qué *harnesses* quiere conectar (y opcionalmente define una carpeta canónica o elige un proyecto).
4. **Simulación (*Dry-run*):** se muestra qué enlaces se crearán y si hay conflictos (por ejemplo, una misma *skill* como carpeta real en dos rutas distintas).
5. **Aplicación:** al pulsar **Aplicar enlaces**, el servidor local crea los *symlinks* necesarios y muestra el resumen de resultados.

## 4. Casos borde y requisitos clave

- **Resolución de rutas reales:** resolver enlaces existentes antes de crear nuevos para evitar *symlinks* anidados o rotos.
- **Conflictos de nombres:** si una *skill* existe como carpeta real en más de un *harness*, marcarlo como conflicto y pedir o aplicar una regla clara (ej. priorizar un origen o mover a carpeta central) en lugar de sobrescribir.
- **Compatibilidad entre SO:** contemplar diferencias en creación de enlaces simbólicos en Windows (Modo Desarrollador / permisos) frente a macOS y Linux.
- **Cierre limpio:** detener el servidor local fácilmente (`Ctrl + C` o botón de salir en la web) sin dejar procesos colgados.

## 5. Estructura del repositorio

```text
harness-skills-linker/
├── SPEC.md
├── app.py                 # Punto de entrada
├── backend/
│   ├── catalog.json       # Catálogo de harnesses y rutas
│   ├── catalog.py         # Carga y expansión de rutas del catálogo
│   ├── server.py          # Servidor HTTP local + API JSON
│   ├── scanner.py         # Detección de harnesses y lectura de skills
│   └── linker.py          # Cálculo de plan (dry-run) y creación de symlinks
├── frontend/
│   ├── index.html         # Interfaz web
│   ├── styles.css         # Estilos
│   └── app.js             # Lógica de UI y llamadas a la API local
└── tests/
```

## 6. Próximos pasos (MVP)

- [ ] Definir el catálogo inicial de harnesses y rutas por defecto (global y proyecto).
- [ ] Implementar el escáner y el cálculo de plan en modo simulación (dry-run).
- [ ] Crear la API local y la interfaz web para seleccionar harnesses y ver el preview.
- [ ] Añadir ejecución real de enlaces, manejo de conflictos y pruebas en macOS/Linux/Windows.
