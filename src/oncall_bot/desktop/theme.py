"""界面配色与样式表。单独放一个文件，免得 gui.py 被 QSS 字符串淹掉。

设计取向：浅色、低饱和、信息分层。原来的界面把十几个等宽按钮平铺在页面上，
谁都看不出该先点哪个；现在的规则是——
  一页最多一个主按钮（蓝底），其余是次要按钮（白底描边），危险操作单独描红。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase

# 调色板
BG = "#f4f5f7"
SURFACE = "#ffffff"
BORDER = "#e2e5ea"
TEXT = "#1f2328"
MUTED = "#6b7280"
ACCENT = "#2563eb"
ACCENT_HOVER = "#1d4ed8"
SUCCESS = "#16a34a"
WARNING = "#d97706"
DANGER = "#dc2626"
SELECTION = "#e8efff"

# 任务 / 会话状态 → 颜色，用于列表里的状态点
STATE_COLORS = {
    "running": ACCENT,
    "queued": WARNING,
    "held": WARNING,
    "unknown": WARNING,
    "done": SUCCESS,
    "sent": SUCCESS,
    "failed": DANGER,
    "cancelled": MUTED,
    "retry": WARNING,
    "pending": MUTED,
}

_FONT_CANDIDATES = ("msyh.ttc", "msyhl.ttc", "simhei.ttf")

STYLESHEET = f"""
QWidget {{
    background: {BG};
    color: {TEXT};
}}
QMainWindow, QDialog {{
    background: {BG};
}}

/* 标签和纯布局容器必须透明，否则放进白卡片里会显示成一条灰底色带 */
QLabel, QCheckBox {{
    background: transparent;
}}
#plain {{
    background: transparent;
}}

/* 左侧导航 */
#nav {{
    background: {SURFACE};
    border: none;
    border-right: 1px solid {BORDER};
    outline: none;
    padding: 8px 0;
}}
#nav::item {{
    padding: 11px 16px;
    margin: 3px 8px;
    border-radius: 8px;
    color: {MUTED};
    min-height: 20px;
}}
#nav::item:selected {{
    background: {SELECTION};
    color: {ACCENT};
    font-weight: 600;
}}
#nav::item:hover:!selected {{
    background: {BG};
}}

/* 卡片容器 */
#card {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
#cardTitle {{
    font-size: 13px;
    font-weight: 600;
    color: {TEXT};
}}
#cardHint, #hint {{
    color: {MUTED};
    font-size: 12px;
}}
#pageTitle {{
    font-size: 18px;
    font-weight: 600;
}}

/* 顶部状态条 */
#statusBarCard {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
#statusMain {{
    font-size: 13px;
    font-weight: 600;
}}
#statusMeta {{
    color: {MUTED};
    font-size: 12px;
}}

/* 按钮：默认是次要样式 */
QPushButton {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 7px 14px;
    min-height: 18px;
    color: {TEXT};
}}
QPushButton:hover {{
    border-color: #c7ccd4;
    background: #fafbfc;
}}
QPushButton:pressed {{
    background: #f0f1f3;
}}
QPushButton:disabled {{
    color: #a6acb6;
    background: #f7f8f9;
}}
QPushButton[kind="primary"] {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton[kind="primary"]:hover {{
    background: {ACCENT_HOVER};
    border-color: {ACCENT_HOVER};
}}
QPushButton[kind="danger"] {{
    color: {DANGER};
    border-color: #f0c2c2;
}}
QPushButton[kind="danger"]:hover {{
    background: #fdf2f2;
    border-color: {DANGER};
}}
QPushButton[kind="ghost"] {{
    background: transparent;
    border: none;
    color: {ACCENT};
    padding: 6px 8px;
}}
QPushButton[kind="ghost"]:hover {{
    background: {SELECTION};
}}

/* 输入框 */
QLineEdit {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 7px 10px;
    selection-background-color: {SELECTION};
    selection-color: {TEXT};
}}
QLineEdit:focus {{
    border-color: {ACCENT};
}}
QLineEdit:read-only {{
    background: #f7f8f9;
    color: {MUTED};
}}

/* 不要自定义 ::indicator：一旦覆盖就连原生勾号一起没了，只会剩个纯色方块 */
QCheckBox {{
    spacing: 8px;
}}

/* 表格 */
QTableWidget {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
    gridline-color: transparent;
    outline: none;
}}
QTableWidget::item {{
    padding: 7px 10px;
    border-bottom: 1px solid #f1f2f4;
}}
QTableWidget::item:selected {{
    background: {SELECTION};
    color: {TEXT};
}}
QHeaderView::section {{
    background: #fafbfc;
    color: {MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 8px 10px;
    font-weight: 600;
}}
QTableCornerButton::section {{
    background: #fafbfc;
    border: none;
}}

QTextBrowser {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 10px;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 4px 2px;
}}
QScrollBar::handle:vertical {{
    background: #ccd1d9;
    border-radius: 5px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: #b3bac4;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px 4px;
}}
QScrollBar::handle:horizontal {{
    background: #ccd1d9;
    border-radius: 5px;
    min-width: 28px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

QStatusBar {{
    background: {SURFACE};
    border-top: 1px solid {BORDER};
    color: {MUTED};
}}
QStatusBar::item {{
    border: none;
}}

QToolTip {{
    background: {TEXT};
    color: #ffffff;
    border: none;
    padding: 5px 8px;
}}
"""


def apply_theme(app) -> None:
    """装样式表，并把界面字体换成系统中文字体。

    离屏渲染时 Qt 不读系统字体目录，必须显式加载，否则中文全是豆腐块。
    """
    for name in _FONT_CANDIDATES:
        path = Path(r"C:\Windows\Fonts") / name
        if not path.exists():
            continue
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id == -1:
            continue
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            family = next((f for f in families if f.endswith("UI")), families[0])
            app.setFont(QFont(family, 10))
            break
    app.setStyleSheet(STYLESHEET)
