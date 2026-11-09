import os
from pathlib import Path

SOCKET = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "usb-panopticon.sock"
