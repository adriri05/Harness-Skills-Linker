# Harness Skills Linker

Web local para detectar los harnesses de agentes instalados en el equipo y enlazar sus skills con symlinks, sin duplicar carpetas.

Estado: en desarrollo. Ver [SPEC.md](SPEC.md) y el [milestone MVP](https://github.com/adriri05/Harness-Skills-Linker/milestone/1).

## Requisitos

- Python 3.10 o superior, sin dependencias externas.
- Windows: Modo Desarrollador activado o terminal con privilegios para crear symlinks.

## Arranque

```bash
python app.py
```

El servidor escucha solo en `127.0.0.1` y abre el navegador.
