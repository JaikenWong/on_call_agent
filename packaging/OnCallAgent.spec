# Build on the target macOS architecture. Use the user's installed Claude CLI.
import os
from pathlib import Path
from PyInstaller.utils.hooks import copy_metadata

root = Path(SPECPATH).parent
a = Analysis(
    [str(root / "packaging/desktop_entry.py")], pathex=[str(root / "src")],
    datas=copy_metadata("claude-agent-sdk") + copy_metadata("lark-oapi"),
    hiddenimports=["keyring.backends.macOS", "lark_oapi.ws.client"],
    excludes=["PySide6.QtWebEngineWidgets","pytest","tkinter"],
)
# SDK hooks may collect the vendor CLI as package data. It must never shadow cli_path.
a.datas = [entry for entry in a.datas if "/_bundled/" not in entry[0]]
a.binaries = [entry for entry in a.binaries if "/_bundled/" not in entry[0]]
pyz = PYZ(a.pure)
identity = os.environ.get("ONCALL_CODESIGN_IDENTITY") or None
exe = EXE(pyz,a.scripts,[],exclude_binaries=True,name="OnCallAgent",console=False,codesign_identity=identity)
coll = COLLECT(exe,a.binaries,a.datas,name="OnCallAgent")
app = BUNDLE(coll,name="OnCallAgent.app",bundle_identifier="local.oncall.agent",
             info_plist={"CFBundleDisplayName":"飞书值班助手","CFBundleShortVersionString":"0.1.0"})
