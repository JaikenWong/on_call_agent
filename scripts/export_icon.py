"""把 desktop/icon.py 里画的图标导出成文件，给打包用。

图标本体是代码画的（见 src/oncall_bot/desktop/icon.py），这个脚本只是把它
落成 PyInstaller 需要的 .ico / .png。改了画法之后重跑一次即可：

    python scripts/export_icon.py

默认写到 packaging/ 下。需要无显示环境时自动走离屏渲染。
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def png_bytes(size: int) -> bytes:
    """单个尺寸的 PNG 字节。"""
    from PySide6.QtCore import QBuffer, QByteArray  # noqa: PLC0415

    from oncall_bot.desktop.icon import app_pixmap  # noqa: PLC0415

    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    app_pixmap(size).save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def build_ico(entries: list[tuple[int, bytes]]) -> bytes:
    """手写多尺寸 ICO。

    QPixmap.save(..., "ICO") 只会写入一张图，而 Windows 的任务栏、资源管理器、
    Alt-Tab 各取不同尺寸，只给 256 会被缩得发虚。ICO 结构很简单：
    6 字节头 + 每项 16 字节目录 + 各自的 PNG 数据（Vista 以上支持 PNG 负载）。
    """
    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = len(header) + 16 * len(entries)
    directory = b""
    payload = b""
    for size, data in entries:
        directory += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,  # 256 在这个字段里记作 0
            0 if size >= 256 else size,
            0,  # 调色板数量，真彩色填 0
            0,  # 保留
            1,  # 色彩平面
            32,  # 位深
            len(data),
            offset,
        )
        payload += data
        offset += len(data)
    return header + directory + payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "packaging"), help="输出目录")
    args = parser.parse_args()

    from PySide6.QtWidgets import QApplication  # noqa: PLC0415

    from oncall_bot.desktop.icon import ICON_SIZES, app_pixmap  # noqa: PLC0415

    app = QApplication.instance() or QApplication([])
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    ico_path = out / "OnCallAgent.ico"
    ico_path.write_bytes(build_ico([(size, png_bytes(size)) for size in ICON_SIZES]))
    print(f"已写入 {ico_path}（{len(ICON_SIZES)} 个尺寸，{ico_path.stat().st_size // 1024} KB）")

    png_path = out / "OnCallAgent.png"
    app_pixmap(512).save(str(png_path), "PNG")
    print(f"已写入 {png_path}")

    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
