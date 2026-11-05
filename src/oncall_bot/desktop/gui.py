"""Qt owns the window and its child engine. All actions return over the pipe.

界面结构：左侧导航 + 右侧页面栈，顶部一条常驻状态条。
原来是三个标签页、每页平铺十来个等宽按钮，看不出主次；现在每页最多一个主按钮，
危险操作单独描红，列表改成带列头的表格。IPC 协议和 op 名称一个都没动。
"""

import hashlib
import json
import os
import shutil
import sys
import uuid

from PySide6.QtCore import QLockFile, QProcess, QSize, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from . import icon, theme
from .runtime import data_root, launch_args

NAV_ITEMS = ("会话", "任务", "设置")


def button(label, handler, kind="secondary", tip=""):
    widget = QPushButton(label)
    widget.setProperty("kind", kind)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.clicked.connect(lambda _checked=False: handler())
    if tip:
        widget.setToolTip(tip)
    return widget


def row(*widgets, stretch_at_end=True):
    """一排按钮。默认右侧留白，这样按钮不会被拉成等宽。"""
    holder = QWidget()
    holder.setObjectName("plain")  # 透明，否则在白卡片上会显示成灰底色带
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    for widget in widgets:
        layout.addWidget(widget)
    if stretch_at_end:
        layout.addStretch()
    return holder


class Card(QFrame):
    """带标题的白底卡片。用它给页面分区，替代原来一坨平铺控件。"""

    def __init__(self, title="", hint=""):
        super().__init__()
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 14, 16, 14)
        self.body.setSpacing(10)
        if title:
            label = QLabel(title)
            label.setObjectName("cardTitle")
            self.body.addWidget(label)
        if hint:
            note = QLabel(hint)
            note.setObjectName("cardHint")
            note.setWordWrap(True)
            self.body.addWidget(note)

    def add(self, widget):
        self.body.addWidget(widget)
        return widget

    def add_field(self, label, widget, trailing=None):
        """一行表单：左侧固定宽度的标签 + 输入框（可选尾部按钮）。"""
        holder = QWidget()
        holder.setObjectName("plain")
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        name = QLabel(label)
        name.setMinimumWidth(96)
        name.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(name)
        layout.addWidget(widget, 1)
        if trailing is not None:
            layout.addWidget(trailing)
        self.body.addWidget(holder)
        return widget


def restyle(widget):
    """改过动态属性后必须重新走一遍样式，否则 QSS 不生效。"""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class StepCard(Card):
    """带序号和完成状态的步骤卡。设置页按使用顺序排，而不是按引擎的 op 清单排。"""

    def __init__(self, number, title, hint=""):
        super().__init__()
        header = QWidget()
        header.setObjectName("plain")
        bar = QHBoxLayout(header)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(10)

        self.badge = QLabel(str(number))
        self.badge.setProperty("badge", "todo")
        self.badge.setFixedSize(22, 22)
        bar.addWidget(self.badge)

        label = QLabel(title)
        label.setObjectName("cardTitle")
        bar.addWidget(label)
        bar.addStretch()

        self.chip = QLabel("待完成")
        self.chip.setProperty("chip", "todo")
        bar.addWidget(self.chip)
        self.body.insertWidget(0, header)

        if hint:
            note = QLabel(hint)
            note.setObjectName("cardHint")
            note.setWordWrap(True)
            self.body.insertWidget(1, note)

    def set_state(self, chip_text, chip_kind):
        self.chip.setText(chip_text)
        self.chip.setProperty("chip", chip_kind)
        self.badge.setProperty("badge", "done" if chip_kind == "done" else "todo")
        restyle(self.chip)
        restyle(self.badge)


class Table(QTableWidget):
    def __init__(self, headers, stretch_column):
        super().__init__(0, len(headers))
        self.setHorizontalHeaderLabels(headers)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setAlternatingRowColors(False)
        header = self.horizontalHeader()
        header.setHighlightSections(False)
        for index in range(len(headers)):
            mode = (
                QHeaderView.ResizeMode.Stretch
                if index == stretch_column
                else QHeaderView.ResizeMode.ResizeToContents
            )
            header.setSectionResizeMode(index, mode)

    def fill(self, rows, columns):
        """rows 是快照里的字典列表，columns 是 (取值函数, 颜色函数|None) 列表。"""
        self.setRowCount(len(rows))
        for r, record in enumerate(rows):
            for c, (getter, colorer) in enumerate(columns):
                item = QTableWidgetItem(str(getter(record)))
                if colorer is not None:
                    tint = colorer(record)
                    if tint:
                        item.setForeground(QColor(tint))
                self.setItem(r, c, item)


def state_color(value):
    return theme.STATE_COLORS.get(str(value), theme.TEXT)


def describe_route(route, empty="未配对"):
    """引擎里的身份是 [app_id, tenant, sender_open_id, chat_id, ""]。

    原样 str() 出来是一串带引号的数组，界面上没法读，这里只摘出能辨认的字段。
    """
    if not route:
        return empty
    if isinstance(route, str):
        return route
    try:
        sender = route[2] or "?"
        chat = route[3] or "?"
    except (IndexError, TypeError):
        return str(route)
    return f"{sender}（会话 {chat}）"


# 引擎里 health 的取值是固定的几个中文串，这里映射成颜色和一句人话。
# 不在界面里用「包含某个词」去猜，引擎改文案时这张表会直接对不上，比猜错好发现。
HEALTH_VIEW = {
    "未连接": (theme.MUTED, "未连接飞书"),
    "应用身份已验证，等待飞书事件": (theme.ACCENT, "飞书长连接已建立"),
    "已收到飞书事件": (theme.SUCCESS, "飞书长连接正常"),
    "长连接结束，请重启": (theme.DANGER, "长连接已断开"),
    "连接失败，请检查凭据与网络": (theme.DANGER, "连接失败"),
    "飞书投递失败，请查看任务": (theme.WARNING, "结果投递失败"),
}


def health_view(health, connected):
    known = HEALTH_VIEW.get(str(health))
    if known:
        return known
    return (theme.ACCENT if connected else theme.MUTED, str(health))


class StatusStrip(QFrame):
    """常驻状态条：引擎 / 接单 / 当前会话，右侧一个暂停开关。"""

    def __init__(self, on_toggle_pause):
        super().__init__()
        self.setObjectName("statusBarCard")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        self.dot = QLabel()
        self.dot.setFixedSize(12, 12)
        layout.addWidget(self.dot)

        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.main = QLabel("引擎启动中")
        self.main.setObjectName("statusMain")
        self.meta = QLabel("—")
        self.meta.setObjectName("statusMeta")
        texts.addWidget(self.main)
        texts.addWidget(self.meta)
        layout.addLayout(texts, 1)

        self.pause = button("暂停接单", on_toggle_pause)
        layout.addWidget(self.pause)
        self.set_state(theme.MUTED, "引擎启动中", "正在连接本机引擎")

    def set_state(self, color, main, meta):
        self.dot.setPixmap(icon.status_dot(color, 12))
        self.main.setText(main)
        self.meta.setText(meta)


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("飞书值班助手")
        self.resize(940, 640)
        self.setMinimumSize(820, 560)
        self.snap = {}
        self.pending = {}
        self.buffer = b""
        self.quitting = False
        self.restarts = 0
        self.suppress_selection = False

        self.proc = QProcess(self)
        self.proc.readyReadStandardOutput.connect(self.read)
        self.proc.readyReadStandardError.connect(self.drain_error)
        self.proc.finished.connect(self.finished)

        self.pages = QStackedWidget()
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(148)
        self.nav.addItems(NAV_ITEMS)
        # QListWidget 的行高只认 sizeHint，QSS 里 ::item 的 padding 撑不开它
        for index in range(self.nav.count()):
            self.nav.item(index).setSizeHint(QSize(0, 42))
        # 三个固定项，不该出现滚动；切页时 autoScroll 会把第一项顶出可视区
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav.setAutoScroll(False)
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)

        self.status_strip = StatusStrip(self.toggle_pause)

        right = QVBoxLayout()
        right.setContentsMargins(18, 16, 18, 16)
        right.setSpacing(14)
        right.addWidget(self.status_strip)
        right.addWidget(self.pages, 1)
        right_holder = QWidget()
        right_holder.setLayout(right)

        shell = QHBoxLayout()
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        shell.addWidget(self.nav)
        shell.addWidget(right_holder, 1)
        central = QWidget()
        central.setLayout(shell)
        self.setCentralWidget(central)

        self.pages.addWidget(self.build_sessions_page())
        self.pages.addWidget(self.build_tasks_page())
        self.pages.addWidget(self.build_setup_page())
        self.nav.setCurrentRow(0)

        self.setWindowIcon(icon.app_icon())
        self.tray = QSystemTrayIcon(icon.tray_icon(theme.MUTED), self)
        menu = QMenu(self)
        menu.addAction("显示窗口", self.reopen)
        menu.addAction("退出应用", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda _: self.reopen())
        self.tray.show()

        self.statusBar().showMessage("就绪")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1200)
        self.start()

    # ---------- 页面 ----------

    def build_sessions_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        title = QLabel("会话")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        hint = QLabel("接管前请先结束终端里的同一会话；释放之后才能回到终端继续用。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.sessions = Table(["", "标题", "会话 ID", "项目路径"], 1)
        layout.addWidget(self.sessions, 1)

        layout.addWidget(
            row(
                button("新建会话", lambda: self.register(False), "primary", "选一个项目目录，开一条全新会话"),
                button("接管已有历史", lambda: self.register(True), tip="接管终端里已经退出的历史会话"),
            )
        )

        selected = Card(
            "选中会话的操作",
            "下面的操作都作用于表格里选中的那一行。",
        )
        selected.add(
            row(
                button("接管", lambda: self.session_command("session.acquire"), "primary"),
                button("释放", lambda: self.call("session.release", self.revision())),
                button("已核对异常", lambda: self.session_command("session.review")),
            )
        )
        selected.add(
            row(
                button("设为只读", lambda: self.set_policy(False)),
                button("允许编辑", lambda: self.set_policy(True)),
                button("撤销项目授权", lambda: self.session_command("project.revoke"), "danger"),
            )
        )
        layout.addWidget(selected)
        return page

    def build_tasks_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        title = QLabel("任务")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        self.tasks = Table(["#", "状态", "投递", "摘要"], 3)
        self.tasks.itemSelectionChanged.connect(self.show_task)

        self.detail = QTextBrowser()
        self.detail.setPlaceholderText("选中一个任务查看详情")

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.tasks)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)

        layout.addWidget(
            row(
                button("取消任务", lambda: self.task_command("task.cancel"), "danger"),
                button("补发结果", lambda: self.task_command("delivery.retry")),
                button("恢复冻结任务", lambda: self.task_command("task.resume_held")),
            )
        )
        return page

    def build_setup_page(self):
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        title = QLabel("设置")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        self.step_access = StepCard(
            1,
            "接入飞书与本机 CLI",
            "填飞书自建应用的凭据和本机 claude 可执行文件路径，保存后引擎才会去建立长连接。\n"
            "Secret 存进系统凭据库，不写配置文件；留空表示沿用上次保存的。",
        )
        self.app_id = QLineEdit()
        self.app_id.setPlaceholderText("cli_ 开头的应用 ID")
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret.setPlaceholderText("留空表示不修改")
        self.secret_eye = button("显示", self.toggle_secret, "ghost", "临时显示 Secret 以便核对")
        self.cli = QLineEdit(shutil.which("claude") or "")
        self.cli.setPlaceholderText("claude 可执行文件的绝对路径")
        self.step_access.add_field("应用 ID", self.app_id)
        self.step_access.add_field("应用 Secret", self.secret, self.secret_eye)
        self.step_access.add_field("Claude CLI", self.cli, button("浏览…", self.pick_cli, "ghost"))
        self.docs = QCheckBox("允许读取消息链接中的飞书文档")
        self.step_access.add(self.docs)
        self.conn_detail = QLabel()
        self.conn_detail.setObjectName("cardHint")
        self.conn_detail.setWordWrap(True)
        self.step_access.add(self.conn_detail)
        self.step_access.add(
            row(
                button("保存并连接", self.configure, "primary"),
                button(
                    "CLI 自检",
                    lambda: self.call(
                        "setup.check",
                        {"cli": self.cli.text()},
                        lambda v: QMessageBox.information(self, "CLI 自检", str(v)),
                    ),
                    tip="跑一次 claude --version，确认路径可执行",
                ),
            )
        )
        layout.addWidget(self.step_access)

        pairing = StepCard(
            2,
            "配对本人身份",
            "把一条飞书私聊认成「本人」，之后只有这个身份能驱动会话。\n"
            "先点「生成配对命令」，复制它在飞书里私聊机器人发出去，再回来点「确认配对身份」。\n"
            "挑战码 5 分钟内有效，确认也必须在这 5 分钟内完成；必须是私聊，话题里发无效。",
        )
        self.step_pair = pairing
        self.pair = QLineEdit()
        self.pair.setReadOnly(True)
        self.pair.setPlaceholderText("点「生成配对命令」后，把这行命令复制到飞书私聊发给机器人")
        pairing.add_field("配对命令", self.pair, button("复制", self.copy_pair, "ghost"))
        self.owner = QLabel()
        self.owner.setObjectName("cardHint")
        self.owner.setWordWrap(True)
        self.candidate = QLabel()
        self.candidate.setObjectName("cardHint")
        self.candidate.setWordWrap(True)
        pairing.add(self.owner)
        pairing.add(self.candidate)
        pairing.add(
            row(
                button(
                    "生成配对命令",
                    lambda: self.call("pair.begin", {}, self.pair.setText),
                    "primary",
                    "生成一个 5 分钟有效的一次性挑战码",
                ),
                button("确认配对身份", self.confirm_pair, tip="把「候选」写成本人"),
                button(
                    "解绑",
                    self.revoke,
                    "danger",
                    "会取消所有在跑和排队的任务，并撤销已登记会话的授权",
                ),
            )
        )
        layout.addWidget(pairing)
        self.render_identity(None, None)

        self.step_project = StepCard(
            3,
            "登记项目与会话",
            "选一个项目目录，新建一条会话或接管终端里已经退出的历史。\n"
            "登记完成后，在飞书私聊里直接发需求就会进任务队列。",
        )
        self.project_detail = QLabel()
        self.project_detail.setObjectName("cardHint")
        self.project_detail.setWordWrap(True)
        self.step_project.add(self.project_detail)
        self.step_project.add(
            row(button("去「会话」页登记", lambda: self.nav.setCurrentRow(0), "primary"))
        )
        layout.addWidget(self.step_project)

        upkeep = Card("维护")
        upkeep.add(
            row(
                button("导入旧配置", self.import_old),
                button("导出诊断", self.export_diag),
                button("清理过期数据", lambda: self.call("storage.clean")),
            )
        )
        layout.addWidget(upkeep)
        layout.addStretch()

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidget(inner)
        area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        return area

    # ---------- 引擎管道（协议未改） ----------

    def start(self):
        args = launch_args("engine")
        self.proc.start(args[0], args[1:])

    def drain_error(self):
        # User data and SDK output must never be copied into GUI diagnostic logs.
        raw = bytes(self.proc.readAllStandardError())
        if "已有引擎".encode() in raw:
            self.restarts = 3
            self.status_strip.set_state(
                theme.DANGER, "已有引擎在运行", "请使用原窗口，或先退出旧的桥进程"
            )

    def finished(self, *_):
        self.pending.clear()
        self.buffer = b""
        if self.quitting:
            QApplication.instance().quit()
        elif self.restarts < 3:
            self.restarts += 1
            self.status_strip.set_state(theme.WARNING, "引擎异常", "正在重新启动")
            QTimer.singleShot(1000 * self.restarts, self.start)

    def refresh(self):
        if self.proc.state() == QProcess.ProcessState.Running and not any(
            op == "app.snapshot" for op, _ in self.pending.values()
        ):
            self.call("app.snapshot", {}, self.update_view)

    def call(self, op, params=None, callback=None):
        if self.proc.state() != QProcess.ProcessState.Running:
            self.statusBar().showMessage("引擎未连接")
            return
        rid = str(uuid.uuid4())
        self.pending[rid] = (op, callback)
        self.proc.write(
            json.dumps(dict(v=1, type="request", id=rid, op=op, params=params or {})).encode() + b"\n"
        )

    def read(self):
        self.buffer += bytes(self.proc.readAllStandardOutput())
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "hello":
                self.refresh()
                continue
            op, cb = self.pending.pop(msg.get("id"), ("", None))
            if msg.get("ok"):
                if cb:
                    cb(msg["data"])
                elif op:
                    self.statusBar().showMessage("操作完成", 3000)
            else:
                self.statusBar().showMessage(str(msg.get("error")), 8000)

    def revision(self):
        return {"revision": self.snap.get("revision", 0)}

    # ---------- 快照 → 界面 ----------

    def update_view(self, s):
        if not self.snap.get("app") and s["app"]:
            self.app_id.setText(s["app"])
            self.cli.setText(s["cli"])
            self.docs.setChecked(s["docs"])

        connected = bool(s.get("connected"))
        paused = s["paused"]
        color, headline = health_view(s["health"], connected)
        if paused:
            color = theme.WARNING
            headline += "（已暂停接单）"
        self.status_strip.set_state(
            color,
            headline,
            "当前会话 " + (s["sid"] or "未接管") + f"　·　任务 {len(s['tasks'])} 条",
        )
        self.status_strip.pause.setText("恢复接单" if paused else "暂停接单")
        self.tray.setIcon(icon.tray_icon(color))
        self.tray.setToolTip("飞书值班助手 · " + headline)

        self.render_identity(s["owner"], s["candidate"])
        self.update_steps(s, connected, color, headline)

    def update_steps(self, s, connected, color, headline):
        """三张步骤卡的完成状态，全部由快照推导，不猜。"""
        if connected:
            self.step_access.set_state("已连接", "done")
        elif s["app"]:
            self.step_access.set_state("未连接", "error" if color == theme.DANGER else "active")
        else:
            self.step_access.set_state("待填写", "todo")
        self.conn_detail.setText(f"长连接状态：{headline}　（引擎原话：{s['health']}）")

        if s["owner"]:
            self.step_pair.set_state("已配对", "done")
        elif s["candidate"]:
            self.step_pair.set_state("待确认", "warn")
        elif not connected:
            self.step_pair.set_state("需先连接", "todo")
        else:
            self.step_pair.set_state("待配对", "active")

        count = len(s["sessions"])
        if s["sid"]:
            self.step_project.set_state("已接管", "done")
        elif count:
            self.step_project.set_state("待接管", "active")
        else:
            self.step_project.set_state("待登记", "todo")
        if count:
            self.project_detail.setText(
                f"已登记 {count} 个会话，当前接管：{s['sid'] or '无（在「会话」页点「接管」）'}"
            )
        else:
            self.project_detail.setText("还没有登记任何项目。")

        active = s["sid"]
        if self.snap.get("sessions") != s["sessions"]:
            self.refill(
                self.sessions,
                s["sessions"],
                "sid",
                [
                    (lambda r: "●" if r["sid"] == active else "", lambda r: theme.ACCENT),
                    (lambda r: r["title"], None),
                    (lambda r: r["sid"], None),
                    (lambda r: r["path"], None),
                ],
            )
        if self.snap.get("tasks") != s["tasks"]:
            self.refill(
                self.tasks,
                s["tasks"],
                "id",
                [
                    (lambda r: "#" + str(r["id"]), None),
                    (lambda r: r["state"], lambda r: state_color(r["state"])),
                    (lambda r: r["delivery"], lambda r: state_color(r["delivery"])),
                    (lambda r: r["summary"], None),
                ],
            )
        self.snap = s

    def refill(self, table, rows, key, columns):
        """重建表格内容，但保住原来选中的那一行。

        重建期间要屏蔽选中事件：selectRow 会触发任务详情加载，而那一刻 self.snap
        还是旧快照，索引对不上就会去取错的任务。
        """
        index = table.currentRow()
        previous = self.snap.get("sessions" if table is self.sessions else "tasks", [])
        selected = previous[index][key] if 0 <= index < len(previous) else None
        self.suppress_selection = True
        try:
            table.fill(rows, columns)
            for n, record in enumerate(rows):
                if record[key] == selected:
                    table.selectRow(n)
                    break
        finally:
            self.suppress_selection = False

    # ---------- 动作 ----------

    def toggle_pause(self):
        self.call("app.resume" if self.snap.get("paused") else "app.pause")

    def toggle_secret(self):
        hidden = self.secret.echoMode() == QLineEdit.EchoMode.Password
        self.secret.setEchoMode(
            QLineEdit.EchoMode.Normal if hidden else QLineEdit.EchoMode.Password
        )
        self.secret_eye.setText("隐藏" if hidden else "显示")

    def pick_cli(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 Claude CLI")
        if path:
            self.cli.setText(path)

    def render_identity(self, owner, candidate):
        """本人 / 候选。引擎存的是 route 数组，直接 str() 出来没法读，只摘关键字段。"""
        self.owner.setText("本人：" + describe_route(owner, "未配对"))
        if candidate:
            self.candidate.setText(
                "候选：" + describe_route(candidate, "无") + "　← 点「确认配对身份」把它定为本人"
            )
        else:
            self.candidate.setText("候选：无（在飞书私聊里发出配对命令后会出现在这里）")

    def copy_pair(self):
        text = self.pair.text()
        if text:
            QApplication.clipboard().setText(text)
            self.statusBar().showMessage("配对命令已复制", 3000)

    def import_old(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择旧版配置", "", "YAML (*.yaml *.yml)")

        def preview(v):
            summary = "\n".join(f"{k}：{v[k]}" for k in sorted(v))
            if self.ask("导入应用与 CLI 设置？旧会话仅作为候选，需重新授权。\n\n" + summary):
                self.app_id.setText(v["app"])
                if v["cli"]:
                    self.cli.setText(v["cli"])

        if path:
            self.call("setup.import", {"path": path}, preview)

    def export_diag(self):
        def save(v):
            text = json.dumps(v, ensure_ascii=False, indent=2)
            if self.ask("导出以下诊断信息？\n\n" + text):
                path, _ = QFileDialog.getSaveFileName(self, "导出诊断", "", "JSON (*.json)")
                if path:
                    with open(path, "w", encoding="utf-8") as out:
                        out.write(text)

        self.call("diagnostic.get", {}, save)

    def configure(self):
        self.call(
            "setup.save",
            dict(
                app=self.app_id.text(),
                secret=self.secret.text(),
                cli=self.cli.text(),
                docs=self.docs.isChecked(),
            ),
        )
        self.secret.clear()

    def confirm_pair(self):
        candidate = self.snap.get("candidate")
        if not candidate:
            QMessageBox.information(self, "配对", "还没有候选身份。先生成配对命令并在飞书里发给机器人。")
            return
        if self.ask("确认将该飞书私聊配对为本人？\n\n" + str(candidate)):
            self.call("pair.confirm", {"candidate": candidate})

    def revoke(self):
        if self.ask("解绑将中止运行任务并撤销远程会话权限。继续？"):
            self.call("pair.revoke")

    def ask(self, text):
        return QMessageBox.question(self, "确认操作", text) == QMessageBox.StandardButton.Yes

    def register(self, existing):
        path = QFileDialog.getExistingDirectory(self, "选择可信项目")
        if not path:
            return
        edit = self.ask("允许在此项目内编辑文件？选「No」则只给只读权限。")
        params = dict(path=path, edit=edit, **self.revision())
        if not existing:
            self.call("session.new", params)
            return

        def choose(rows):
            labels = [f"{r['sid']}  {r['title']}" for r in rows]
            if not labels:
                QMessageBox.information(self, "历史", "该项目没有可接管的历史")
                return
            item, ok = QInputDialog.getItem(self, "会话历史", "请选择已退出终端的会话", labels, 0, False)
            if ok and self.ask("确认终端已退出，允许把此历史的回答发回本人飞书？"):
                self.call("session.import", dict(params, sid=rows[labels.index(item)]["sid"]))

        self.call("session.discover", {"path": path}, choose)

    def current_session(self):
        """从表格自身读会话 ID，不靠「行号 + 快照」间接定位，避免两者不同步时取错行。"""
        index = self.sessions.currentRow()
        item = self.sessions.item(index, 2) if index >= 0 else None
        return item.text() if item is not None else None

    def current_task_no(self):
        index = self.tasks.currentRow()
        item = self.tasks.item(index, 0) if index >= 0 else None
        if item is None:
            return None
        try:
            return int(item.text().lstrip("#"))
        except ValueError:
            return None

    def session_command(self, op):
        sid = self.current_session()
        if sid is None:
            self.statusBar().showMessage("先在表格里选中一条会话", 4000)
            return
        if self.ask("已核对终端和项目，继续执行此操作？"):
            self.call(op, dict(sid=sid, **self.revision()))

    def set_policy(self, edit):
        sid = self.current_session()
        if sid is None:
            self.statusBar().showMessage("先在表格里选中一条会话", 4000)
            return
        if self.ask("更新该项目所有会话的工具权限？已冻结任务需重新核验权限。"):
            self.call("project.set_policy", dict(sid=sid, edit=edit, **self.revision()))

    def task_command(self, op, cb=None):
        task_no = self.current_task_no()
        if task_no is None:
            self.statusBar().showMessage("先在表格里选中一个任务", 4000)
            return
        self.call(op, {"task_no": task_no}, cb)

    def show_task(self):
        if self.suppress_selection:
            return
        task_no = self.current_task_no()
        if task_no is None:
            return
        self.call(
            "task.get",
            {"task_no": task_no},
            lambda r: self.detail.setPlainText(
                f"#{r['id']}　{r['state']}\n{r['snapshot']['path']}\n\n"
                f"{r['snapshot']['text']}\n\n——\n\n{r['result']}"
            ),
        )

    # ---------- 窗口生命周期 ----------

    def reopen(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit(self):
        if self.quitting or not self.ask("退出引擎并冻结队列？运行中的任务可能需要核对。"):
            return
        self.quitting = True
        self.timer.stop()
        if self.proc.state() == QProcess.ProcessState.NotRunning:
            QApplication.instance().quit()
        else:
            self.call("app.shutdown")
            QTimer.singleShot(10000, self.proc.kill)

    def closeEvent(self, event):
        if self.quitting:
            event.accept()
            return
        event.ignore()
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.hide()
        else:
            self.quit()


def run():
    app = QApplication([])
    theme.apply_theme(app)
    app.setQuitOnLastWindowClosed(False)
    root = data_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = QLockFile(str(root / "window.lock"))
    address = (
        "oncall-window-" + hashlib.sha256(str(root).encode()).hexdigest()[:20]
        if os.name == "nt"
        else str(root / "window.sock")
    )
    if not lock.tryLock(0):
        socket = QLocalSocket()
        socket.connectToServer(address)
        socket.waitForConnected(500)
        return 0
    server = QLocalServer()
    QLocalServer.removeServer(address)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    server.listen(address)
    window = Window()

    def activate():
        while server.hasPendingConnections():
            connection = server.nextPendingConnection()
            connection.disconnectFromServer()
            connection.deleteLater()
        window.reopen()

    server.newConnection.connect(activate)
    window.show()
    if "--check-ui" in sys.argv:

        def check_engine():
            if window.snap and not window.quitting:
                window.quitting = True
                window.timer.stop()
                window.call("app.shutdown")

        QTimer.singleShot(30000, lambda: app.exit(2))
        timer = QTimer(window)
        timer.timeout.connect(check_engine)
        timer.start(200)
    return app.exec()
