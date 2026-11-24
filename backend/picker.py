"""Selector nativo de carpeta.

El navegador no expone rutas absolutas de un <input type="file">, y la API
las exige. Como el servidor corre en la misma máquina, abre el diálogo del
sistema aquí. Tkinter se ejecuta en un subproceso porque necesita su propio
hilo principal (en macOS falla desde un hilo secundario del servidor).
"""

from __future__ import annotations

import os
import subprocess
import sys

TIMEOUT_SECONDS = 600

_SCRIPT = """
import sys
import tkinter
from tkinter import filedialog

root = tkinter.Tk()
root.withdraw()
root.attributes("-topmost", True)
chosen = filedialog.askdirectory(title=sys.argv[1], mustexist=True)
root.destroy()
sys.stdout.buffer.write(chosen.encode("utf-8"))
"""


class PickerUnavailable(Exception):
    """No se pudo abrir el diálogo nativo en este equipo."""


def pick_folder(title: str = "Elige una carpeta") -> str | None:
    """Abre el diálogo y devuelve la ruta absoluta, o None si se cancela."""
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", _SCRIPT, title],
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PickerUnavailable(str(exc)) from None
    if proc.returncode != 0:
        raise PickerUnavailable(proc.stderr.decode("utf-8", "replace").strip())
    chosen = proc.stdout.decode("utf-8").strip()
    return os.path.normpath(chosen) if chosen else None
