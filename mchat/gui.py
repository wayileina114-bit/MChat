"""MChat 图形界面：类 Discord 的暗色桌面应用（PySide6）。"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QFrame,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import qasync

from . import __app_name__, __version__
from .config import account, load_config, save_config
from .service import MatrixService
from .updater import ReleaseInfo, check_update_async, is_newer

# --------------------------------------------------------------------------
# 配色（Discord 风格）
# --------------------------------------------------------------------------
C_DARKEST = "#202225"   # 最左侧账号栏
C_SIDEBAR = "#2f3136"   # 侧栏
C_BG = "#36393f"        # 聊天区背景
C_INPUT = "#40444b"     # 输入框
C_TEXT = "#dcddde"      # 正文
C_TEXT_MUTED = "#8e9297"  # 次要文字
C_ACCENT = "#5865f2"    # 强调色（blurple）
C_ONLINE = "#3ba55d"    # 在线绿
C_HOVER = "#3c3f45"

QSS = f"""
* {{
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
    font-size: 13px;
    color: {C_TEXT};
}}
QWidget {{ background: transparent; }}
QMainWindow, QDialog {{ background: {C_BG}; }}

/* 最左侧账号栏 */
#accountRail {{ background: {C_DARKEST}; border: none; }}
#accountBtn {{
    background: {C_BG};
    border-radius: 22px;
    border: 2px solid transparent;
    color: {C_TEXT};
    font-size: 16px;
    font-weight: bold;
}}
#accountBtn:hover {{ border-radius: 16px; background: {C_ACCENT}; }}
#accountBtn:checked {{ border-radius: 16px; border: 2px solid {C_TEXT}; background: {C_ACCENT}; }}

/* 侧栏 */
#sidebar {{ background: {C_SIDEBAR}; border: none; }}
#roomTitle {{ font-size: 15px; font-weight: bold; color: #ffffff; padding: 4px; }}
#channelItem {{
    background: {C_HOVER};
    border-radius: 6px;
    padding: 8px 10px;
    color: #ffffff;
    font-weight: 600;
}}
#mutedLabel {{ color: {C_TEXT_MUTED}; font-size: 11px; }}

/* 聊天区 */
#chatHeader {{
    background: {C_BG};
    border-bottom: 1px solid #26282c;
    padding: 10px 16px;
}}
#chatTitle {{ font-size: 16px; font-weight: bold; color: #ffffff; }}
#chatSub {{ color: {C_TEXT_MUTED}; font-size: 12px; }}

#msgList {{ background: {C_BG}; border: none; }}
#msgList::item {{ border: none; background: transparent; }}
#msgList::item:selected {{ background: transparent; }}

#inputBar {{ background: {C_BG}; border-top: 1px solid #26282c; }}
#msgInput {{
    background: {C_INPUT};
    border: none;
    border-radius: 8px;
    padding: 10px;
    color: {C_TEXT};
    font-size: 14px;
}}
#sendBtn {{
    background: {C_ACCENT};
    color: #ffffff;
    border: none;
    border-radius: 8px;
    padding: 10px 18px;
    font-weight: 600;
}}
#sendBtn:hover {{ background: #4752c4; }}
#sendBtn:pressed {{ background: #3c45a5; }}

QLineEdit {{
    background: {C_INPUT};
    border: 1px solid #202225;
    border-radius: 6px;
    padding: 8px;
    color: {C_TEXT};
}}
QLineEdit:focus {{ border: 1px solid {C_ACCENT}; }}
QDialog QLabel {{ color: {C_TEXT}; }}
"""

# 消息发送者名字 → 稳定颜色
_NAME_COLORS = [
    "#f23f43", "#f0b232", "#f2e03d", "#3ba55d",
    "#46b1d1", "#5865f2", "#9b59b6", "#e91e63",
    "#00bcd4", "#ff9800",
]


def name_color(name: str) -> str:
    h = 0
    for ch in name:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return _NAME_COLORS[h % len(_NAME_COLORS)]


def fmt_time(ts_ms: int) -> str:
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000)
        today = datetime.now().date()
        if dt.date() == today:
            return dt.strftime("%H:%M")
        return dt.strftime("%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------
# 消息条目
# --------------------------------------------------------------------------
class MessageWidget(QWidget):
    def __init__(self, sender_name: str, sender_id: str, body: str, ts_ms: int, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(10)

        # 头像：首字母 + 颜色
        avatar = QLabel(sender_name[:1].upper() if sender_name else "?")
        avatar.setFixedSize(40, 40)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setStyleSheet(
            f"background: {name_color(sender_id)}; color: white;"
            f"border-radius: 20px; font-size: 16px; font-weight: bold;"
        )
        lay.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(2)

        head = QHBoxLayout()
        head.setSpacing(8)
        name_lbl = QLabel(sender_name or sender_id)
        name_lbl.setStyleSheet(
            f"color: {name_color(sender_id)}; font-weight: bold; font-size: 14px;"
        )
        time_lbl = QLabel(fmt_time(ts_ms))
        time_lbl.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 11px;")
        head.addWidget(name_lbl)
        head.addWidget(time_lbl)
        head.addStretch(1)
        col.addLayout(head)

        body_lbl = QLabel(body)
        body_lbl.setWordWrap(True)
        body_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body_lbl.setStyleSheet(f"color: {C_TEXT}; font-size: 14px;")
        col.addWidget(body_lbl)

        lay.addLayout(col, 1)


# --------------------------------------------------------------------------
# 登录/配置对话框
# --------------------------------------------------------------------------
class LoginDialog(QDialog):
    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{__app_name__} — 首次配置")
        self.setMinimumWidth(460)
        self.cfg = cfg

        lay = QVBoxLayout(self)
        title = QLabel("配置两个 Matrix 账号")
        title.setStyleSheet("font-size: 18px; font-weight: bold; color: #ffffff;")
        lay.addWidget(title)
        sub = QLabel("程序A 和 程序B 各登录一个账号，在端到端加密房间里互相通信。")
        sub.setStyleSheet(f"color: {C_TEXT_MUTED};")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        form = QFormLayout()
        form.setSpacing(10)

        self.homeserver = QLineEdit(cfg.get("homeserver", "https://matrix.org"))
        self.proxy = QLineEdit(cfg.get("proxy", ""))
        self.proxy.setPlaceholderText("可选，例如 http://127.0.0.1:7890")

        acct_a = cfg["accounts"]["a"]
        acct_b = cfg["accounts"]["b"]
        self.a_user = QLineEdit(acct_a.get("user_id", ""))
        self.a_user.setPlaceholderText("@username:matrix.org")
        self.a_pass = QLineEdit(acct_a.get("password", ""))
        self.a_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.b_user = QLineEdit(acct_b.get("user_id", ""))
        self.b_user.setPlaceholderText("@username:matrix.org")
        self.b_pass = QLineEdit(acct_b.get("password", ""))
        self.b_pass.setEchoMode(QLineEdit.EchoMode.Password)

        form.addRow("Homeserver", self.homeserver)
        form.addRow("代理（可选）", self.proxy)
        form.addRow("程序A 用户名", self.a_user)
        form.addRow("程序A 密码", self.a_pass)
        form.addRow("程序B 用户名", self.b_user)
        form.addRow("程序B 密码", self.b_pass)
        lay.addLayout(form)

        btn = QPushButton("连接")
        btn.setStyleSheet(
            f"background: {C_ACCENT}; color: white; border: none; border-radius: 6px;"
            "padding: 10px; font-weight: 600;"
        )
        btn.clicked.connect(self._on_ok)
        lay.addWidget(btn)

    def _on_ok(self):
        if not (self.a_user.text().strip() and self.a_pass.text().strip()):
            QMessageBox.warning(self, "提示", "请填写程序A 的用户名和密码")
            return
        if not (self.b_user.text().strip() and self.b_pass.text().strip()):
            QMessageBox.warning(self, "提示", "请填写程序B 的用户名和密码")
            return
        self.cfg["homeserver"] = self.homeserver.text().strip() or "https://matrix.org"
        self.cfg["proxy"] = self.proxy.text().strip()
        self.cfg["accounts"]["a"]["user_id"] = self.a_user.text().strip()
        self.cfg["accounts"]["a"]["password"] = self.a_pass.text().strip()
        self.cfg["accounts"]["b"]["user_id"] = self.b_user.text().strip()
        self.cfg["accounts"]["b"]["password"] = self.b_pass.text().strip()
        save_config(self.cfg)
        self.accept()


# --------------------------------------------------------------------------
# 更新检查（跨线程安全）
# --------------------------------------------------------------------------
class UpdateChecker(QObject):
    found = Signal(object)

    def check(self):
        def cb(info):
            self.found.emit(info)
        check_update_async(cb)


# --------------------------------------------------------------------------
# 消息输入框（Enter 发送，Shift+Enter 换行）
# --------------------------------------------------------------------------
class MessageInput(QPlainTextEdit):
    submitted = Signal()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            e.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.submitted.emit()
            return
        super().keyPressEvent(e)


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------
class MainWindow(QWidget):
    def __init__(self, service: MatrixService):
        super().__init__()
        self.service = service
        self.current_key = "a"
        self._started = False

        self.setWindowTitle(f"{__app_name__} v{__version__}")
        self.resize(1000, 680)

        service.on_message = self._on_message
        service.on_status = self._on_status

        self._build_ui()

        self.updater = UpdateChecker()
        self.updater.found.connect(self._on_update_found)

        QTimer.singleShot(0, lambda: asyncio.create_task(self._startup()))

    # ---------------- UI 构建 ----------------
    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 最左：账号栏
        rail = QFrame()
        rail.setObjectName("accountRail")
        rail.setFixedWidth(72)
        rail_lay = QVBoxLayout(rail)
        rail_lay.setContentsMargins(12, 12, 12, 12)
        rail_lay.setSpacing(12)
        rail_lay.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.btn_a = self._make_account_btn("A", "a")
        self.btn_b = self._make_account_btn("B", "b")
        self.btn_a.setChecked(True)
        rail_lay.addWidget(self.btn_a)
        rail_lay.addWidget(self.btn_b)
        rail_lay.addStretch(1)
        about = QPushButton("?")
        about.setFixedSize(44, 44)
        about.setObjectName("accountBtn")
        about.setToolTip(f"{__app_name__} v{__version__}")
        about.clicked.connect(self._show_about)
        rail_lay.addWidget(about)
        root.addWidget(rail)

        # 侧栏
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        side_lay = QVBoxLayout(sidebar)
        side_lay.setContentsMargins(10, 12, 10, 12)
        side_lay.setSpacing(8)

        room_title = QLabel("MChat 通道")
        room_title.setObjectName("roomTitle")
        side_lay.addWidget(room_title)

        channel = QLabel("#  MChat 通道")
        channel.setObjectName("channelItem")
        side_lay.addWidget(channel)

        side_lay.addStretch(1)

        self.status_a = QLabel("程序A：未连接")
        self.status_b = QLabel("程序B：未连接")
        self.status_a.setObjectName("mutedLabel")
        self.status_b.setObjectName("mutedLabel")
        side_lay.addWidget(self.status_a)
        side_lay.addWidget(self.status_b)
        root.addWidget(sidebar)

        # 聊天区
        chat = QWidget()
        chat_lay = QVBoxLayout(chat)
        chat_lay.setContentsMargins(0, 0, 0, 0)
        chat_lay.setSpacing(0)

        header = QFrame()
        header.setObjectName("chatHeader")
        header_lay = QVBoxLayout(header)
        header_lay.setContentsMargins(16, 8, 16, 8)
        header_lay.setSpacing(2)
        self.chat_title = QLabel("MChat 通道")
        self.chat_title.setObjectName("chatTitle")
        self.chat_sub = QLabel("正在连接……")
        self.chat_sub.setObjectName("chatSub")
        header_lay.addWidget(self.chat_title)
        header_lay.addWidget(self.chat_sub)
        chat_lay.addWidget(header)

        self.msg_list = QListWidget()
        self.msg_list.setObjectName("msgList")
        self.msg_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.msg_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.msg_list.setSpacing(0)
        chat_lay.addWidget(self.msg_list, 1)

        input_bar = QFrame()
        input_bar.setObjectName("inputBar")
        input_lay = QHBoxLayout(input_bar)
        input_lay.setContentsMargins(16, 12, 16, 12)
        input_lay.setSpacing(10)
        self.input = MessageInput()
        self.input.setObjectName("msgInput")
        self.input.setPlaceholderText("发消息给房间（以当前身份）…… Enter 发送，Shift+Enter 换行")
        self.input.setFixedHeight(72)
        self.input.submitted.connect(self._send)
        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("sendBtn")
        self.send_btn.clicked.connect(self._send)
        input_lay.addWidget(self.input, 1)
        input_lay.addWidget(self.send_btn, 0, Qt.AlignmentFlag.AlignBottom)
        chat_lay.addWidget(input_bar)

        root.addWidget(chat, 1)

    def _make_account_btn(self, text: str, key: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("accountBtn")
        btn.setFixedSize(48, 48)
        btn.setCheckable(True)
        btn.setToolTip(f"以「{account(load_config(), key)['label']}」身份发送")
        btn.clicked.connect(lambda _=False, k=key: self._switch_key(k))
        self.group.addButton(btn)
        return btn

    # ---------------- 启动 ----------------
    async def _startup(self):
        cfg = load_config()
        if not self._is_configured(cfg):
            dlg = LoginDialog(cfg, self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                QApplication.quit()
                return
            cfg = load_config()

        self.chat_sub.setText("正在登录两个账号……")
        try:
            await self.service.connect("a")
            await self.service.connect("b")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "登录失败", str(exc))
            self.chat_sub.setText("登录失败，请检查配置")
            return

        # 首次需要初始化房间
        if not self.service.room_id:
            self.chat_sub.setText("首次运行：正在创建加密房间并握手……")
            try:
                await self.service.setup_room()
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "初始化失败", str(exc))
                self.chat_sub.setText("初始化失败")
                return

        self.chat_sub.setText("已连接 · 端到端加密已启用")
        await self._load_history()

        # 启动更新检查
        self.updater.check()

    @staticmethod
    def _is_configured(cfg: dict) -> bool:
        a = cfg["accounts"]["a"]
        b = cfg["accounts"]["b"]
        return bool(
            a.get("user_id")
            and b.get("user_id")
            and (a.get("password") or a.get("access_token"))
            and (b.get("password") or b.get("access_token"))
        )

    async def _load_history(self):
        try:
            msgs = await self.service.history("a", limit=50)
            for ev in reversed(msgs):
                self._append_message(
                    ev.sender,
                    getattr(ev, "body", ""),
                    getattr(ev, "server_timestamp", 0),
                )
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 消息回调 ----------------
    def _on_message(self, key, label, sender_name, sender_id, body, ts_ms):
        # 只显示文本，忽略握手/系统提示之外的自己回声也正常显示
        self._append_message(sender_id, body, ts_ms)

    def _append_message(self, sender_id, body, ts_ms):
        cfg = load_config()
        name = self._display_name(cfg, sender_id)
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, 60))
        widget = MessageWidget(name, sender_id, body, ts_ms)
        self.msg_list.addItem(item)
        self.msg_list.setItemWidget(item, widget)
        # 限制条数，避免无限增长
        while self.msg_list.count() > 500:
            self.msg_list.takeItem(0)
        self.msg_list.scrollToBottom()

    def _display_name(self, cfg: dict, sender_id: str) -> str:
        for key in ("a", "b"):
            acct = cfg["accounts"][key]
            if acct.get("user_id") == sender_id:
                return acct.get("label", key)
        return sender_id

    def _on_status(self, key, text):
        label = account(load_config(), key)["label"]
        if key == "a":
            self.status_a.setText(f"{label}：{text}")
        elif key == "b":
            self.status_b.setText(f"{label}：{text}")

    # ---------------- 交互 ----------------
    def _switch_key(self, key):
        self.current_key = key
        label = account(load_config(), key)["label"]
        self.input.setPlaceholderText(
            f"以「{label}」身份发消息…… Enter 发送，Shift+Enter 换行"
        )

    def _send(self):
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        asyncio.create_task(self._do_send(text))

    async def _do_send(self, text):
        try:
            await self.service.send_text(self.current_key, text)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "发送失败", str(exc))

    def _on_update_found(self, info):
        if info and is_newer(info.tag_name, __version__):
            self.chat_sub.setText(
                f"有新版本 {info.tag_name} 可用，点击前往下载"
            )
            self.chat_sub.setStyleSheet(f"color: #f0b232; font-size: 12px;")
            if QMessageBox.question(
                self,
                "发现新版本",
                f"当前版本 {__version__}，最新版本 {info.tag_name}。\n\n"
                f"{info.body[:200]}\n\n是否打开下载页面？",
            ) == QMessageBox.StandardButton.Yes:
                import webbrowser
                webbrowser.open(info.html_url)

    def _show_about(self):
        QMessageBox.information(
            self,
            "关于",
            f"{__app_name__} v{__version__}\n\n"
            "基于 Matrix 协议的端到端加密聊天应用。\n"
            "让两个你自己的程序通过加密房间互相通信。",
        )

    def closeEvent(self, e):
        if self.service.clients:
            asyncio.create_task(self.service.close())
        super().closeEvent(e)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(__app_name__)
    app.setStyleSheet(QSS)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    service = MatrixService()
    window = MainWindow(service)
    window.show()

    app_close_event = asyncio.Event()
    app.aboutToQuit.connect(app_close_event.set)

    with loop:
        loop.run_until_complete(app_close_event.wait())
    return 0
