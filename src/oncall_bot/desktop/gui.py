"""Qt owns the window and its child engine. All actions return over the pipe."""

import hashlib
import json
import os
import shutil
import sys
import uuid

from PySide6.QtCore import QLockFile, QProcess, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStyle,
    QSystemTrayIcon,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .runtime import data_root, launch_args


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("飞书值班助手")
        self.resize(800, 560)
        self.snap = {}
        self.pending = {}
        self.buffer = b""
        self.quitting = False
        self.restarts = 0
        self.proc = QProcess(self)
        self.proc.readyReadStandardOutput.connect(self.read)
        self.proc.readyReadStandardError.connect(self.drain_error)
        self.proc.finished.connect(self.finished)
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        setup = self.page("接入与配对")
        self.health = QLabel("引擎启动中")
        setup.addWidget(self.health)
        form = QFormLayout()
        setup.addLayout(form)
        self.app_id = QLineEdit()
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.cli = QLineEdit(shutil.which("claude") or "")
        self.docs = QCheckBox("允许读取消息链接中的飞书文档")
        form.addRow("应用 ID", self.app_id)
        form.addRow("应用 Secret", self.secret)
        form.addRow("Claude CLI", self.cli)
        setup.addWidget(self.docs)
        self.buttons(setup, [("选择 CLI", self.pick_cli), ("保存并连接", self.configure)])
        self.buttons(
            setup,
            [
                (
                    "CLI 自检",
                    lambda: self.call(
                        "setup.check",
                        {"cli": self.cli.text()},
                        lambda v: QMessageBox.information(self, "CLI", v),
                    ),
                ),
                ("导入旧配置", self.import_old),
                ("导出诊断", self.export_diag),
                ("清理过期数据", lambda: self.call("storage.clean")),
            ],
        )
        self.pair = QLineEdit()
        self.pair.setReadOnly(True)
        setup.addWidget(self.pair)
        self.owner = QLabel()
        self.owner.setWordWrap(True)
        setup.addWidget(self.owner)
        self.buttons(
            setup,
            [
                ("生成配对命令", lambda: self.call("pair.begin", {}, self.pair.setText)),
                ("确认配对身份", self.confirm_pair),
                ("解绑", self.revoke),
            ],
        )
        setup.addStretch()
        projects = self.page("项目与会话")
        projects.addWidget(QLabel("接管前请先结束终端里的同一会话。释放后才可在终端续接。"))
        self.sessions = QListWidget()
        projects.addWidget(self.sessions)
        self.buttons(
            projects,
            [
                ("新建会话", lambda: self.register(False)),
                ("接管已有历史", lambda: self.register(True)),
                ("接管选中项", lambda: self.session_command("session.acquire")),
                ("释放", lambda: self.call("session.release", self.revision())),
            ],
        )
        self.buttons(
            projects,
            [
                ("已核对异常", lambda: self.session_command("session.review")),
                ("撤销项目授权", lambda: self.session_command("project.revoke")),
            ],
        )
        tasks = self.page("任务")
        self.tasks = QListWidget()
        tasks.addWidget(self.tasks)
        self.detail = QTextBrowser()
        tasks.addWidget(self.detail)
        self.buttons(
            tasks,
            [
                ("查看", self.show_task),
                ("取消", lambda: self.task_command("task.cancel")),
                ("补发", lambda: self.task_command("delivery.retry")),
                ("恢复冻结任务", lambda: self.task_command("task.resume_held")),
            ],
        )
        self.buttons(
            tasks,
            [("暂停接单", lambda: self.call("app.pause")), ("恢复接单", lambda: self.call("app.resume"))],
        )
        self.tray = QSystemTrayIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon), self)
        menu = QMenu(self)
        menu.addAction("显示窗口", self.reopen)
        menu.addAction("退出应用", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda _: self.reopen())
        self.tray.show()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1200)
        self.start()

    def page(self, name):
        page = QWidget()
        self.tabs.addTab(page, name)
        return QVBoxLayout(page)

    def buttons(self, layout, items):
        row = QHBoxLayout()
        layout.addLayout(row)
        for label, func in items:
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, f=func: f())
            row.addWidget(button)

    def start(self):
        args = launch_args("engine")
        self.proc.start(args[0], args[1:])

    def drain_error(self):
        # User data and SDK output must never be copied into GUI diagnostic logs.
        raw = bytes(self.proc.readAllStandardError())
        if "已有引擎".encode() in raw:
            self.restarts = 3
            self.health.setText("已有引擎运行，请使用原窗口或退出旧桥")

    def finished(self, *_):
        self.pending.clear()
        self.buffer = b""
        if self.quitting:
            QApplication.instance().quit()
        elif self.restarts < 3:
            self.restarts += 1
            self.health.setText("引擎异常，正在重新启动")
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

    def update_view(self, s):
        if not self.snap.get("app") and s["app"]:
            self.app_id.setText(s["app"])
            self.cli.setText(s["cli"])
            self.docs.setChecked(s["docs"])
        self.health.setText(
            f"{s['health']}  ·  {'暂停' if s['paused'] else '接单开启'}  ·  当前会话 {s['sid'] or '未接管'}"
        )
        self.tray.setToolTip(self.health.text())
        self.owner.setText("本人：" + str(s["owner"] or "未配对") + "\n候选：" + str(s["candidate"] or "无"))
        for widget, key, field, label in (
            (self.sessions, "sessions", "sid", lambda r: f"{r['title']}  {r['sid']}  {r['path']}"),
            (
                self.tasks,
                "tasks",
                "id",
                lambda r: f"#{r['id']}  {r['state']}  ·  投递 {r['delivery']}  {r['summary']}",
            ),
        ):
            if self.snap.get(key) != s[key]:
                index = widget.currentRow()
                previous = self.snap.get(key, [])
                selected = previous[index][field] if 0 <= index < len(previous) else None
                widget.clear()
                widget.addItems([label(r) for r in s[key]])
                for n, r in enumerate(s[key]):
                    if r[field] == selected:
                        widget.setCurrentRow(n)
        self.snap = s

    def pick_cli(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 Claude CLI")
        if path:
            self.cli.setText(path)

    def import_old(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择旧版配置", "", "YAML (*.yaml *.yml)")

        def preview(v):
            if self.ask(
                "导入应用与 CLI 设置？旧会话仅作为候选，需重新授权。\n" + json.dumps(v, ensure_ascii=False)
            ):
                self.app_id.setText(v["app"])
                if v["cli"]:
                    self.cli.setText(v["cli"])

        if path:
            self.call("setup.import", {"path": path}, preview)

    def export_diag(self):
        def save(v):
            text = json.dumps(v, ensure_ascii=False, indent=2)
            if self.ask("导出以下诊断信息？\n" + text):
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
        if candidate and self.ask("确认将该飞书私聊配对为本人？\n" + str(candidate)):
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
        edit = self.ask("允许在此项目内编辑文件？选择 No 将使用只读权限。")
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

    def session_command(self, op):
        n = self.sessions.currentRow()
        if n >= 0 and self.ask("已核对终端和项目，继续执行此操作？"):
            self.call(op, dict(sid=self.snap["sessions"][n]["sid"], **self.revision()))

    def task_command(self, op, cb=None):
        n = self.tasks.currentRow()
        if n >= 0:
            self.call(op, {"task_no": self.snap["tasks"][n]["id"]}, cb)

    def show_task(self):
        self.task_command(
            "task.get",
            lambda r: self.detail.setPlainText(
                f"#{r['id']}  {r['state']}\n{r['snapshot']['path']}\n{r['snapshot']['text']}\n\n{r['result']}"
            ),
        )

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
