import atexit
import os
import shutil
import tempfile

# Ningún test debe escribir el historial en el home real del usuario.
_HOME = tempfile.mkdtemp(prefix="hsl-history-")
os.environ["HARNESS_LINKER_HOME"] = _HOME
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
