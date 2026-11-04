# The GUI has no console. The engine uses its own console-subsystem launcher
# so Python's stdin/stdout are real handles when Qt starts it with pipes.
from pathlib import Path
from PyInstaller.utils.hooks import copy_metadata

root = Path(SPECPATH).parent
a = Analysis(
    [str(root / "packaging/desktop_entry.py")], pathex=[str(root / "src")],
    datas=copy_metadata("claude-agent-sdk") + copy_metadata("lark-oapi"),
    hiddenimports=["keyring.backends.Windows", "win32timezone", "lark_oapi.ws.client"],
    excludes=["pytest", "tkinter", "PySide6.QtWebEngineWidgets"],
)
a.datas = [entry for entry in a.datas if "_bundled/" not in entry[0].replace("\\","/")]
a.binaries = [entry for entry in a.binaries if "_bundled/" not in entry[0].replace("\\","/")]
pyz = PYZ(a.pure)
gui = EXE(pyz,a.scripts,[],exclude_binaries=True,name="OnCallAgent",console=False)
engine = EXE(pyz,a.scripts,[],exclude_binaries=True,name="OnCallEngine",console=True)
coll = COLLECT(gui,engine,a.binaries,a.datas,name="OnCallAgent")
