# Build natively on Linux. The onedir archive preserves executable bits and symlinks.
from pathlib import Path
from PyInstaller.utils.hooks import copy_metadata

root = Path(SPECPATH).parent
a = Analysis(
    [str(root / "packaging/desktop_entry.py")], pathex=[str(root / "src")],
    datas=copy_metadata("claude-agent-sdk") + copy_metadata("lark-oapi"),
    hiddenimports=["keyring.backends.SecretService", "lark_oapi.ws.client"],
    excludes=["pytest", "tkinter", "PySide6.QtWebEngineWidgets"],
)
a.datas = [entry for entry in a.datas if "/_bundled/" not in entry[0]]
a.binaries = [entry for entry in a.binaries if "/_bundled/" not in entry[0]]
pyz = PYZ(a.pure)
exe = EXE(pyz,a.scripts,[],exclude_binaries=True,name="OnCallAgent",console=True)
coll = COLLECT(exe,a.binaries,a.datas,name="OnCallAgent")
