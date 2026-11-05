"""应用图标，用 QPainter 画出来，不依赖任何图片资源。

好处是任意尺寸都清晰、仓库里不用放二进制，打包时用 scripts/export_icon.py
导出 .ico/.png 给 PyInstaller。

图形：圆角方块底 + 白色对话气泡 + 气泡里一个终端提示符 `>`。
含义是「聊天窗口驱动命令行」，16px 下也还能认出来。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from . import theme

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def app_pixmap(size: int = 256) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    unit = size / 256.0

    # 底：圆角方块
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(theme.ACCENT)))
    painter.drawRoundedRect(QRectF(0, 0, size, size), 56 * unit, 56 * unit)

    # 对话气泡
    bubble = QRectF(44 * unit, 56 * unit, 168 * unit, 118 * unit)
    path = QPainterPath()
    path.addRoundedRect(bubble, 30 * unit, 30 * unit)
    tail = QPainterPath()
    tail.moveTo(QPointF(86 * unit, 170 * unit))
    tail.lineTo(QPointF(86 * unit, 214 * unit))
    tail.lineTo(QPointF(132 * unit, 172 * unit))
    tail.closeSubpath()
    painter.setBrush(QBrush(QColor("#ffffff")))
    painter.drawPath(path.united(tail))

    # 气泡里的终端提示符：> 和一条下划线
    pen = QPen(QColor(theme.ACCENT))
    pen.setWidthF(16 * unit)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    chevron = QPainterPath()
    chevron.moveTo(QPointF(86 * unit, 92 * unit))
    chevron.lineTo(QPointF(120 * unit, 115 * unit))
    chevron.lineTo(QPointF(86 * unit, 138 * unit))
    painter.drawPath(chevron)
    painter.drawLine(QPointF(136 * unit, 138 * unit), QPointF(174 * unit, 138 * unit))

    painter.end()
    return pixmap


def app_icon() -> QIcon:
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(app_pixmap(size))
    return icon


def status_dot(color: str, size: int = 12) -> QPixmap:
    """状态圆点，用在状态条上。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    return pixmap


def tray_icon(color: str, size: int = 64) -> QIcon:
    """托盘图标 = 应用图标 + 右下角状态点。

    比原来那个光秃秃的状态圆点好：既能认出是哪个应用，也能看出当前状态。
    """
    pixmap = app_pixmap(size)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    badge = size * 0.42
    x = size - badge - size * 0.04
    y = size - badge - size * 0.04
    painter.setPen(QPen(QColor("#ffffff"), size * 0.06))
    painter.setBrush(QColor(color))
    painter.drawEllipse(QRectF(x, y, badge, badge))
    painter.end()
    return QIcon(pixmap)
