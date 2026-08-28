"""MChat 图形界面：类 Discord 的 Matrix 聊天客户端（单账号，与真人通讯）。"""
from __future__ import annotations

import asyncio
import html as html_mod
import os
import re
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap, QImage, QPainter, QBrush, QFont, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

import qasync

from . import __app_name__, __version__
from .config import is_configured, load_config, save_config
from .service import MatrixService
from .updater import ReleaseInfo, check_update_async, download_asset, is_newer

# --------------------------------------------------------------------------
# 配色（Discord 风格）
# --------------------------------------------------------------------------
C_DARKEST = "#202225"
C_SIDEBAR = "#2f3136"
C_BG = "#36393f"
C_INPUT = "#40444b"
C_TEXT = "#dcddde"
C_TEXT_MUTED = "#8e9297"
C_ACCENT = "#5865f2"
C_ONLINE = "#3ba55d"
C_HOVER = "#3c3f45"

QSS = f"""
* {{
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
    font-size: 13px;
    color: {C_TEXT};
}}
QWidget {{ background: transparent; }}
QDialog {{ background: {C_BG}; }}

#sidebar {{ background: {C_SIDEBAR}; border: none; }}
#userLabel {{ font-size: 14px; font-weight: bold; color: #ffffff; }}
#userSub {{ color: {C_TEXT_MUTED}; font-size: 11px; }}
#sectionLabel {{ color: {C_TEXT_MUTED}; font-size: 11px; font-weight: bold; padding-top: 6px; }}

#roomList {{ background: {C_SIDEBAR}; border: none; }}
#roomList::item {{ color: {C_TEXT}; padding: 8px 10px; border-radius: 6px; }}
#roomList::item:selected {{ background: {C_ACCENT}; color: #ffffff; }}
#roomList::item:hover {{ background: {C_HOVER}; }}

#chatHeader {{ background: {C_BG}; border-bottom: 1px solid #26282c; padding: 10px 16px; }}
#chatTitle {{ font-size: 16px; font-weight: bold; color: #ffffff; }}
#chatSub {{ color: {C_TEXT_MUTED}; font-size: 12px; }}

#msgList {{ background: {C_BG}; border: none; }}
#msgList::item {{ border: none; background: transparent; }}

#inputBar {{ background: {C_BG}; border-top: 1px solid #26282c; }}
#msgInput {{
    background: {C_INPUT}; border: none; border-radius: 8px;
    padding: 10px; color: {C_TEXT}; font-size: 14px;
}}
#sendBtn {{
    background: {C_ACCENT}; color: #ffffff; border: none; border-radius: 8px;
    padding: 10px 18px; font-weight: 600;
}}
#sendBtn:hover {{ background: #4752c4; }}

QLineEdit {{
    background: {C_INPUT}; border: 1px solid #202225; border-radius: 6px;
    padding: 8px; color: {C_TEXT};
}}
QLineEdit:focus {{ border: 1px solid {C_ACCENT}; }}
QDialog QLabel {{ color: {C_TEXT}; }}
"""

_NAME_COLORS = [
    "#f23f43", "#f0b232", "#f2e03d", "#3ba55d",
    "#46b1d1", "#5865f2", "#9b59b6", "#e91e63",
    "#00bcd4", "#ff9800",
]


_URL_RE = re.compile(r"(https?://[^\s<]+)")


def linkify(text: str) -> str:
    """把文本里的 URL 转成可点击的 <a> 链接（先做 HTML 转义防注入）。"""
    escaped = html_mod.escape(text)
    return _URL_RE.sub(r'<a href="\1">\1</a>', escaped)


def name_color(name: str) -> str:
    h = 0
    for ch in name:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return _NAME_COLORS[h % len(_NAME_COLORS)]


def fmt_time(ts_ms: int) -> str:
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000)
        now = datetime.now()
        delta = now - dt
        if delta < timedelta(minutes=1):
            return "刚刚"
        if delta < timedelta(hours=1):
            return f"{int(delta.total_seconds() // 60)} 分钟前"
        if dt.date() == now.date():
            return dt.strftime("%H:%M")
        if dt.date() == (now - timedelta(days=1)).date():
            return "昨天 " + dt.strftime("%H:%M")
        return dt.strftime("%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------
# 消息条目
# --------------------------------------------------------------------------
class ClickableLabel(QLabel):
    clicked = Signal()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


class MessageWidget(QWidget):
    def __init__(self, sender_name: str, sender_id: str, body: str, ts_ms: int, parent=None, is_placeholder: bool = False, image_url: str | None = None, is_own: bool = False):
        super().__init__(parent)
        self.image_label = None
        self._image_data = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(10)

        self.avatar = QLabel(sender_name[:1].upper() if sender_name else "?")
        self.avatar.setFixedSize(40, 40)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.avatar.setStyleSheet(
            f"background: {name_color(sender_id)}; color: white;"
            f"border-radius: 20px; font-size: 16px; font-weight: bold;"
        )
        lay.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(2)

        head = QHBoxLayout()
        head.setSpacing(8)
        name_lbl = QLabel(sender_name or sender_id)
        name_lbl.setStyleSheet(f"color: {name_color(sender_id)}; font-weight: bold; font-size: 14px;")
        self._ts_ms = ts_ms
        self.time_lbl = QLabel(fmt_time(ts_ms))
        self.time_lbl.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 11px;")
        try:
            self.time_lbl.setToolTip(datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:  # noqa: BLE001
            pass
        head.addWidget(name_lbl)
        self.status_label = None
        if is_own:
            self.status_label = QLabel("已发送")
            self.status_label.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 11px;")
            head.addWidget(self.status_label)
        head.addWidget(time_lbl)
        head.addStretch(1)
        col.addLayout(head)

        if image_url:
            self.image_label = ClickableLabel("🖼️ 图片加载中……")
            self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.image_label.setMinimumSize(160, 100)
            self.image_label.setStyleSheet(
                f"background: {C_INPUT}; border-radius: 8px; color: {C_TEXT_MUTED};"
            )
            self.image_label.clicked.connect(self._open_image)
            col.addWidget(self.image_label)
        else:
            body_lbl = QLabel()
            body_lbl.setWordWrap(True)
            body_lbl.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.LinksAccessibleByMouse
            )
            body_lbl.setOpenExternalLinks(True)
            if is_placeholder:
                body_lbl.setText(body)
                body_lbl.setStyleSheet(f"color: {C_TEXT_MUTED}; font-style: italic; font-size: 14px;")
            else:
                body_lbl.setText(linkify(body))
                body_lbl.setTextFormat(Qt.TextFormat.RichText)
                body_lbl.setStyleSheet(f"color: {C_TEXT}; font-size: 14px;")
            col.addWidget(body_lbl)

        lay.addLayout(col, 1)

    def set_status(self, text: str):
        if self.status_label:
            self.status_label.setText(text)

    def update_time(self):
        if getattr(self, "time_lbl", None):
            self.time_lbl.setText(fmt_time(self._ts_ms))

    def set_image(self, data: bytes):
        label = getattr(self, "image_label", None)
        if not label or not data:
            return
        self._image_data = data
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            label.setText("🖼️ 图片无法显示")
            return
        scaled = pixmap.scaledToWidth(260, Qt.TransformationMode.SmoothTransformation)
        label.setPixmap(scaled)
        label.setFixedSize(scaled.size())
        label.setStyleSheet("")
        label.setToolTip("点击查看原图")

    def set_avatar(self, data: bytes):
        if not data:
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return
        size = 40
        pixmap = pixmap.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
        result = QPixmap(size, size)
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QBrush(pixmap))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(0, 0, size, size)
        painter.end()
        self.avatar.setPixmap(result)
        self.avatar.setStyleSheet("")

    def _open_image(self):
        if not self._image_data:
            return
        path = os.path.join(tempfile.gettempdir(), f"mchat_view_{datetime.now().strftime('%H%M%S%f')}.png")
        with open(path, "wb") as f:
            f.write(self._image_data)
        try:
            os.startfile(path)  # Windows 用系统默认程序打开
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# 登录对话框（单账号）
# --------------------------------------------------------------------------
class LoginDialog(QDialog):
    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{__app_name__} — 登录")
        self.setMinimumWidth(460)
        self.cfg = cfg

        lay = QVBoxLayout(self)
        title = QLabel("登录你的 Matrix 账号")
        title.setStyleSheet("font-size: 18px; font-weight: bold; color: #ffffff;")
        lay.addWidget(title)
        sub = QLabel("登录后即可与好友进行端到端加密聊天。账号可在 element.io 等客户端注册。")
        sub.setStyleSheet(f"color: {C_TEXT_MUTED};")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        form = QFormLayout()
        form.setSpacing(10)
        self.homeserver = QLineEdit(cfg.get("homeserver", "https://matrix.org"))
        self.proxy = QLineEdit(cfg.get("proxy", ""))
        self.proxy.setPlaceholderText("可选，例如 http://127.0.0.1:7890")
        self.user_id = QLineEdit(cfg.get("user_id", ""))
        self.user_id.setPlaceholderText("@username:matrix.org")
        self.password = QLineEdit(cfg.get("password", ""))
        self.password.setEchoMode(QLineEdit.EchoMode.Password)

        form.addRow("Homeserver", self.homeserver)
        form.addRow("代理（可选）", self.proxy)
        form.addRow("用户名", self.user_id)
        form.addRow("密码", self.password)
        lay.addLayout(form)

        btn = QPushButton("登录")
        btn.setStyleSheet(
            f"background: {C_ACCENT}; color: white; border: none; border-radius: 6px;"
            "padding: 10px; font-weight: 600;"
        )
        btn.clicked.connect(self._on_ok)
        lay.addWidget(btn)

    def _on_ok(self):
        if not (self.user_id.text().strip() and self.password.text().strip()):
            QMessageBox.warning(self, "提示", "请填写用户名和密码")
            return
        self.cfg["homeserver"] = self.homeserver.text().strip() or "https://matrix.org"
        self.cfg["proxy"] = self.proxy.text().strip()
        self.cfg["user_id"] = self.user_id.text().strip()
        self.cfg["password"] = self.password.text().strip()
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
    image_pasted = Signal(object)  # QImage

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setMinimumHeight(40)
        self.textChanged.connect(self._auto_resize)

    def _auto_resize(self):
        doc = self.document()
        height = int(doc.size().height()) + 24
        self.setFixedHeight(min(max(height, 40), 130))

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            e.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.submitted.emit()
            return
        super().keyPressEvent(e)

    def insertFromMimeData(self, source):
        # 粘贴的是图片则直接作为图片消息发送，不插入文本
        if source.hasImage():
            image = source.imageData()
            if image and not image.isNull():
                self.image_pasted.emit(image)
                return
        super().insertFromMimeData(source)


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------
class MainWindow(QWidget):
    def __init__(self, service: MatrixService):
        super().__init__()
        self.service = service
        self.current_room_id = ""
        self._reply_to = None
        self._seen_events: set = set()
        self._last_msg_date: str | None = None
        self._messages: list = []
        self._pending_widgets: dict = {}
        self._sent_widgets: dict = {}
        self._history_end_token: "str | None" = None
        self._loading_older = False
        self._started = False

        self.setWindowTitle(f"{__app_name__} v{__version__}")
        self.resize(1050, 700)

        service.on_message = self._on_message
        service.on_room_update = self._refresh_rooms
        service.on_status = self._on_status
        service.on_key_received = self._on_key_received
        service.on_receipt = self._on_receipt
        service.on_typing = self._on_typing
        service.on_mention = self._on_mention

        self._build_ui()

        # 系统托盘：用于新消息通知
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation))
        self.tray.setToolTip(f"{__app_name__} v{__version__}")
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.messageClicked.connect(self._on_notify_clicked)
        self.tray.show()
        self._notify_room = None

        self.updater = UpdateChecker()
        self.updater.found.connect(self._on_update_found)

        # 定期刷新房间列表
        self._room_timer = QTimer(self)
        self._room_timer.timeout.connect(self._refresh_rooms)
        self._room_timer.start(5000)

        # 定期刷新消息相对时间（刚刚 / x分钟前）
        self._time_timer = QTimer(self)
        self._time_timer.timeout.connect(self._refresh_message_times)
        self._time_timer.start(60000)

        QTimer.singleShot(0, lambda: asyncio.create_task(self._startup()))

    # ---------------- UI ----------------
    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 侧栏：账号 + 房间列表 + 按钮
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(250)
        side_lay = QVBoxLayout(sidebar)
        side_lay.setContentsMargins(10, 12, 10, 12)
        side_lay.setSpacing(6)

        self.user_label = QLabel("未登录")
        self.user_label.setObjectName("userLabel")
        self.user_sub = QLabel("正在连接……")
        self.user_sub.setObjectName("userSub")
        side_lay.addWidget(self.user_label)
        side_lay.addWidget(self.user_sub)

        side_lay.addSpacing(8)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("搜索房间……")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self._refresh_rooms)
        side_lay.addWidget(self.search_box)
        group_lbl = QLabel("群聊")
        group_lbl.setObjectName("sectionLabel")
        side_lay.addWidget(group_lbl)
        self.group_list = QListWidget()
        self.group_list.setObjectName("roomList")
        self.group_list.itemClicked.connect(self._on_room_clicked)
        self.group_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.group_list.customContextMenuRequested.connect(self._show_room_context_menu)
        side_lay.addWidget(self.group_list, 1)

        dm_lbl = QLabel("私聊")
        dm_lbl.setObjectName("sectionLabel")
        side_lay.addWidget(dm_lbl)
        self.dm_list = QListWidget()
        self.dm_list.setObjectName("roomList")
        self.dm_list.itemClicked.connect(self._on_room_clicked)
        self.dm_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.dm_list.customContextMenuRequested.connect(self._show_room_context_menu)
        side_lay.addWidget(self.dm_list, 1)

        # 底部按钮
        btn_row = QHBoxLayout()
        self.add_btn = QPushButton("＋ 新建")
        self.add_btn.setStyleSheet(self._btn_style())
        self.add_btn.clicked.connect(self._show_new_menu)
        self.join_btn = QPushButton("加入")
        self.join_btn.setStyleSheet(self._btn_style())
        self.join_btn.clicked.connect(self._join_room)
        btn_row.addWidget(self.add_btn)
        btn_row.addWidget(self.join_btn)
        side_lay.addLayout(btn_row)
        self.logout_btn = QPushButton("登出")
        self.logout_btn.setStyleSheet(self._btn_style())
        self.logout_btn.clicked.connect(self._logout)
        side_lay.addWidget(self.logout_btn)
        root.addWidget(sidebar)

        # 聊天区
        chat = QWidget()
        chat_lay = QVBoxLayout(chat)
        chat_lay.setContentsMargins(0, 0, 0, 0)
        chat_lay.setSpacing(0)

        header = QFrame()
        header.setObjectName("chatHeader")
        header_lay = QHBoxLayout(header)
        header_lay.setContentsMargins(16, 8, 16, 8)
        header_text = QVBoxLayout()
        header_text.setSpacing(2)
        self.chat_title = QLabel("选择一个房间开始聊天")
        self.chat_title.setObjectName("chatTitle")
        self.chat_sub = QLabel("登录后，左侧会显示你加入的房间")
        self.chat_sub.setObjectName("chatSub")
        header_text.addWidget(self.chat_title)
        header_text.addWidget(self.chat_sub)
        header_lay.addLayout(header_text, 1)
        self.members_btn = QPushButton("成员")
        self.members_btn.setStyleSheet(self._btn_style())
        self.members_btn.clicked.connect(self._show_members)
        self.members_btn.setEnabled(False)
        header_lay.addWidget(self.members_btn)
        self.search_btn = QPushButton("🔍")
        self.search_btn.setToolTip("搜索消息")
        self.search_btn.setStyleSheet(self._btn_style())
        self.search_btn.clicked.connect(self._search_message)
        self.search_btn.setEnabled(False)
        header_lay.addWidget(self.search_btn)
        self.invite_btn = QPushButton("邀请好友")
        self.invite_btn.setStyleSheet(self._btn_style())
        self.invite_btn.clicked.connect(self._invite)
        self.invite_btn.setEnabled(False)
        header_lay.addWidget(self.invite_btn)
        chat_lay.addWidget(header)

        self.msg_list = QListWidget()
        self.msg_list.setObjectName("msgList")
        self.msg_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.msg_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.msg_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.msg_list.customContextMenuRequested.connect(self._show_msg_context_menu)
        self.msg_list.verticalScrollBar().valueChanged.connect(self._on_scroll)
        chat_lay.addWidget(self.msg_list, 1)

        self.jump_btn = QPushButton("↓ 跳到最新消息")
        self.jump_btn.setStyleSheet(
            f"background: {C_ACCENT}; color: white; border: none; border-radius: 6px; padding: 4px 10px;"
        )
        self.jump_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.jump_btn.clicked.connect(self._jump_to_bottom)
        self.jump_btn.hide()
        chat_lay.addWidget(self.jump_btn)

        input_bar = QFrame()
        input_bar.setObjectName("inputBar")
        input_bar_lay = QVBoxLayout(input_bar)
        input_bar_lay.setContentsMargins(0, 0, 0, 0)
        input_bar_lay.setSpacing(0)
        self._reply_label = QLabel("")
        self._reply_label.setStyleSheet(f"color: {C_ACCENT}; font-size: 12px; padding: 4px 16px 0 16px; background: {C_BG};")
        self._reply_label.hide()
        input_bar_lay.addWidget(self._reply_label)
        input_lay = QHBoxLayout()
        input_lay.setContentsMargins(16, 4, 16, 12)
        input_lay.setSpacing(10)
        input_bar_lay.addLayout(input_lay)
        self.emoji_btn = QPushButton("😊")
        self.emoji_btn.setToolTip("表情")
        self.emoji_btn.setStyleSheet(self._btn_style())
        self.emoji_btn.setFixedSize(44, 44)
        self.emoji_btn.clicked.connect(self._show_emoji_menu)
        input_lay.addWidget(self.emoji_btn, 0, Qt.AlignmentFlag.AlignBottom)
        self.image_btn = QPushButton("🖼️")
        self.image_btn.setToolTip("发送图片")
        self.image_btn.setStyleSheet(self._btn_style())
        self.image_btn.setFixedSize(44, 44)
        self.image_btn.setEnabled(False)
        self.image_btn.clicked.connect(self._send_image)
        input_lay.addWidget(self.image_btn, 0, Qt.AlignmentFlag.AlignBottom)
        self.input = MessageInput()
        self.input.setObjectName("msgInput")
        self.input.setPlaceholderText("发消息…… Enter 发送，Shift+Enter 换行")
        self.input.setEnabled(False)
        self.input.submitted.connect(self._send)
        self.input.textChanged.connect(self._on_input_changed)
        self.input.image_pasted.connect(self._on_image_pasted)
        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("sendBtn")
        self.send_btn.clicked.connect(self._send)
        self.send_btn.setEnabled(False)
        input_lay.addWidget(self.input, 1)
        input_lay.addWidget(self.send_btn, 0, Qt.AlignmentFlag.AlignBottom)
        chat_lay.addWidget(input_bar)

        root.addWidget(chat, 1)

    @staticmethod
    def _btn_style() -> str:
        return (
            f"background: {C_INPUT}; color: {C_TEXT}; border: none;"
            "border-radius: 6px; padding: 8px;"
        )

    # ---------------- 启动 ----------------
    async def _startup(self):
        cfg = load_config()
        if not is_configured(cfg):
            dlg = LoginDialog(cfg, self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                QApplication.quit()
                return
            cfg = load_config()

        self.user_label.setText(cfg["user_id"].split(":")[0].lstrip("@"))
        self.user_sub.setText("正在登录……")
        try:
            await self.service.connect()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "登录失败", str(exc))
            self.user_sub.setText("❌ 登录失败，请检查配置")
            self.user_sub.setStyleSheet("color: #f23f43; font-size: 11px;")
            return

        self.user_sub.setText("🟢 已连接 · 端到端加密已启用")
        self.user_sub.setStyleSheet("color: #3ba55d; font-size: 11px;")
        self._refresh_rooms()
        if not self.service.rooms():
            self.chat_title.setText("欢迎使用 MChat")
            self.chat_sub.setText("还没有房间。点左下角「＋ 新建」创建群聊或私聊，或「加入」加入已有房间")
        self.updater.check()

    # ---------------- 房间列表 ----------------
    def _refresh_rooms(self):
        if not self.service.client:
            return
        rooms = self.service.rooms()
        self.group_list.clear()
        self.dm_list.clear()
        keyword = self.search_box.text().strip().lower() if hasattr(self, "search_box") else ""
        for r in rooms:
            if keyword and keyword not in r["display_name"].lower():
                continue
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, r["room_id"])
            item.setSizeHint(QSize(0, 40))
            icon = "#" if not r["is_dm"] else "@"
            name = r["display_name"]
            unread = r.get("unread", 0)
            unread_txt = f"  ({unread})" if unread else ""
            if self.service.is_muted(r["room_id"]):
                name = f"🔕 {name}"
            if self.service.is_pinned(r["room_id"]):
                name = f"📌 {name}"
            lm = r.get("last_message")
            if lm:
                sender, preview = lm
                preview = preview.replace("\n", " ").strip()
                if len(preview) > 18:
                    preview = preview[:18] + "…"
                item.setText(f"{icon} {name}{unread_txt}\n  {sender}: {preview}")
            else:
                item.setText(f"{icon} {name}{unread_txt}")
            if unread:
                f = QFont()
                f.setBold(True)
                item.setFont(f)
            if r["room_id"] == self.current_room_id:
                item.setSelected(True)
            peer = r.get("peer_id")
            if peer:
                asyncio.create_task(self._set_room_avatar(item, peer))
            else:
                asyncio.create_task(self._set_group_avatar(item, r["room_id"]))
            target = self.dm_list if r["is_dm"] else self.group_list
            target.addItem(item)

    def _refresh_message_times(self):
        for i in range(self.msg_list.count()):
            widget = self.msg_list.itemWidget(self.msg_list.item(i))
            if widget and hasattr(widget, "update_time"):
                widget.update_time()

    def _on_room_clicked(self, item):
        room_id = item.data(Qt.ItemDataRole.UserRole)
        if room_id != self.current_room_id:
            self.current_room_id = room_id
            asyncio.create_task(self._load_room(room_id))

    def _show_room_context_menu(self, pos):
        widget = self.sender()
        if widget not in (self.group_list, self.dm_list):
            return
        item = widget.itemAt(pos)
        if not item:
            return
        room_id = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        muted = self.service.is_muted(room_id)
        pinned = self.service.is_pinned(room_id)
        act_pin = menu.addAction("取消置顶" if pinned else "置顶房间")
        act_mute = menu.addAction("取消静音" if muted else "静音房间")
        act_leave = menu.addAction("离开房间")
        chosen = menu.exec(widget.viewport().mapToGlobal(pos))
        if chosen == act_pin:
            self._toggle_pin(room_id)
        elif chosen == act_mute:
            self._toggle_mute(room_id)
        elif chosen == act_leave:
            self._leave_room(room_id)

    def _toggle_pin(self, room_id):
        self.service.toggle_pin(room_id)
        self._refresh_rooms()

    def _toggle_mute(self, room_id):
        muted = self.service.toggle_mute(room_id)
        self._refresh_rooms()

    def _leave_room(self, room_id):
        if QMessageBox.question(self, "离开房间", "确定要离开这个房间吗？") == QMessageBox.StandardButton.Yes:
            asyncio.create_task(self._do_leave_room(room_id))

    async def _do_leave_room(self, room_id):
        try:
            await self.service.leave_room(room_id)
            if self.current_room_id == room_id:
                self.current_room_id = ""
                self.msg_list.clear()
                self._messages = []
                self.chat_title.setText("选择一个房间开始聊天")
                self.chat_sub.setText("")
                self.setWindowTitle(f"{__app_name__} v{__version__}")
            self._refresh_rooms()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "离开失败", str(exc))

    async def _load_room(self, room_id):
        self.msg_list.clear()
        self._seen_events.clear()
        self._last_msg_date = None
        self._messages = []
        self.service.mark_read(room_id)
        self._refresh_rooms()
        # 更新标题
        for r in self.service.rooms():
            if r["room_id"] == room_id:
                self.chat_title.setText(r["display_name"])
                self.chat_sub.setText(f"{r['member_count']} 名成员" + (" · 私聊" if r["is_dm"] else " · 群聊"))
                self.setWindowTitle(f"{__app_name__} v{__version__} · {r['display_name']}")
                break
        self.input.setEnabled(True)
        self.send_btn.setEnabled(True)
        self.invite_btn.setEnabled(True)
        self.members_btn.setEnabled(True)
        self.search_btn.setEnabled(True)
        self.image_btn.setEnabled(True)
        self._history_end_token = None
        try:
            msgs, self._history_end_token = await self.service.history(room_id, limit=100)
            for m in msgs:
                self._append_message(
                    m["event_id"], m["sender"], m["body"], m["ts"], m["decrypted"], m.get("image_url")
                )
            # 发送已读标记：告诉对端已读到最新消息
            if msgs and msgs[-1].get("event_id"):
                asyncio.create_task(self.service.send_read_marker(room_id, msgs[-1]["event_id"]))
        except Exception:  # noqa: BLE001
            pass
        self.msg_list.scrollToBottom()

    # ---------------- 消息 ----------------
    def _on_message(self, room_id, event_id, sender_name, sender_id, body, ts_ms, image_url=None):
        if room_id == self.current_room_id:
            # 自己发送的消息经 sync 确认后，把「发送中」更新为「已发送」
            if event_id in self._pending_widgets:
                w = self._pending_widgets.pop(event_id)
                w.set_status("已发送")
            self._append_message(event_id, sender_id, body, ts_ms, True, image_url)
        else:
            # 非当前房间的新消息：系统托盘通知（过滤自己发 + 历史同步）
            if (
                self.service.client
                and sender_id != self.service.client.user_id
                and ts_ms >= self.service._session_start_ms
                and body != "🔒 无法解密的消息（缺少密钥）"
                and not self.service.is_muted(room_id)
            ):
                room_name = self._room_name(room_id)
                self._notify_room = room_id
                self.tray.showMessage(
                    f"{sender_name} · {room_name}",
                    body,
                    QSystemTrayIcon.MessageIcon.Information,
                    5000,
                )
                try:
                    QApplication.beep()
                except Exception:  # noqa: BLE001
                    pass

    def _append_message(self, event_id, sender_id, body, ts_ms, decrypted=True, image_url=None):
        if event_id and event_id in self._seen_events:
            return
        if event_id:
            self._seen_events.add(event_id)
        day = self._date_key(ts_ms)
        if day != self._last_msg_date:
            self._add_date_separator(day)
            self._last_msg_date = day
        name = self._display_name(sender_id)
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, 60))
        item.setData(Qt.ItemDataRole.UserRole, body)
        item.setData(Qt.ItemDataRole.UserRole + 1, event_id)
        item.setData(Qt.ItemDataRole.UserRole + 2, sender_id)
        self.msg_list.addItem(item)
        is_own = bool(self.service.client and sender_id == self.service.client.user_id)
        widget = MessageWidget(name, sender_id, body, ts_ms, is_placeholder=not decrypted, image_url=image_url, is_own=is_own)
        self.msg_list.setItemWidget(item, widget)
        if is_own and event_id:
            self._sent_widgets[event_id] = widget
        if image_url:
            asyncio.create_task(self._load_image(widget, image_url))
        if sender_id:
            asyncio.create_task(self._load_avatar(widget, sender_id))
        self._messages.append({
            "event_id": event_id, "sender": sender_id, "body": body,
            "ts": ts_ms, "decrypted": decrypted, "image_url": image_url,
        })
        while self.msg_list.count() > 500:
            self.msg_list.takeItem(0)
            if self._messages:
                self._messages.pop(0)
        self.msg_list.scrollToBottom()
        return widget

    async def _load_image(self, widget, image_url):
        data = await self.service.download_media(image_url)
        if data:
            widget.set_image(data)

    def _on_scroll(self, value):
        scrollbar = self.msg_list.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 10
        if hasattr(self, "jump_btn"):
            self.jump_btn.setVisible(not at_bottom)
        if value == 0 and not self._loading_older and self._history_end_token and self.current_room_id:
            asyncio.create_task(self._load_older())

    def _jump_to_bottom(self):
        self.msg_list.scrollToBottom()
        self.jump_btn.hide()

    async def _load_older(self):
        self._loading_older = True
        scrollbar = self.msg_list.verticalScrollBar()
        old_max = scrollbar.maximum()
        old_value = scrollbar.value()
        try:
            msgs, end_token = await self.service.history(
                self.current_room_id, limit=50, from_token=self._history_end_token
            )
            if end_token:
                self._history_end_token = end_token
            if not msgs:
                return
            new_msgs = []
            for m in msgs:
                new_msgs.append({
                    "event_id": m["event_id"], "sender": m["sender"], "body": m["body"],
                    "ts": m["ts"], "decrypted": m["decrypted"], "image_url": m.get("image_url"),
                })
            self._messages = new_msgs + self._messages
            self._rebuild_messages()
            new_max = scrollbar.maximum()
            scrollbar.setValue(new_max - (old_max - old_value))
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._loading_older = False

    def _rebuild_messages(self):
        self.msg_list.clear()
        self._seen_events.clear()
        self._last_msg_date = None
        for m in self._messages:
            self._append_message(m["event_id"], m["sender"], m["body"], m["ts"], m["decrypted"], m.get("image_url"))

    async def _load_avatar(self, widget, sender_id):
        data = await self.service.get_avatar_data(sender_id)
        if data:
            widget.set_avatar(data)

    @staticmethod
    def _date_key(ts_ms: int) -> str:
        try:
            dt = datetime.fromtimestamp(ts_ms / 1000)
            today = datetime.now().date()
            if dt.date() == today:
                return "今天"
            if dt.date() == today - timedelta(days=1):
                return "昨天"
            return dt.strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            return ""

    def _add_date_separator(self, day: str):
        if not day:
            return
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, 28))
        self.msg_list.addItem(item)
        lbl = QLabel(day)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 11px; background: transparent;")
        self.msg_list.setItemWidget(item, lbl)

    def _show_msg_context_menu(self, pos):
        item = self.msg_list.itemAt(pos)
        if not item:
            return
        body = item.data(Qt.ItemDataRole.UserRole)
        if not body:
            return
        event_id = item.data(Qt.ItemDataRole.UserRole + 1)
        sender_id = item.data(Qt.ItemDataRole.UserRole + 2)
        menu = QMenu(self)
        act_copy = menu.addAction("复制消息")
        act_reply = menu.addAction("回复")
        is_own = bool(sender_id and self.service.client and sender_id == self.service.client.user_id)
        is_text = not body.startswith("🖼️") and body != "🔒 无法解密的消息（缺少密钥）"
        act_edit = None
        act_redact = None
        if is_own and is_text:
            act_edit = menu.addAction("编辑消息")
            act_redact = menu.addAction("撤回消息")
        elif is_own:
            act_redact = menu.addAction("撤回消息")
        chosen = menu.exec(self.msg_list.viewport().mapToGlobal(pos))
        if chosen == act_copy:
            QApplication.clipboard().setText(body)
        elif chosen == act_reply:
            self._set_reply(event_id, sender_id, body)
        elif act_edit and chosen == act_edit:
            self._edit_message(event_id, body)
        elif act_redact and chosen == act_redact:
            asyncio.create_task(self._do_redact(event_id))

    def _set_reply(self, event_id, sender_id, body):
        name = self._display_name(sender_id)
        preview = body.replace("\n", " ")[:50]
        self._reply_to = (event_id, name, preview)
        self._reply_label.setText(f"↩ 回复 {name}：{preview}")
        self._reply_label.show()
        self.input.setFocus()

    def _cancel_reply(self):
        self._reply_to = None
        self._reply_label.hide()

    def _edit_message(self, event_id, old_text):
        text, ok = QInputDialog.getText(self, "编辑消息", "新内容：", text=old_text)
        if not ok or not text.strip():
            return
        asyncio.create_task(self._do_edit(event_id, text.strip()))

    async def _do_edit(self, event_id, text):
        try:
            await self.service.edit_message(self.current_room_id, event_id, text)
            await self._load_room(self.current_room_id)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "编辑失败", str(exc))

    async def _do_redact(self, event_id):
        try:
            await self.service.redact(self.current_room_id, event_id)
            await self._load_room(self.current_room_id)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "撤回失败", str(exc))

    def _display_name(self, sender_id):
        # 在房间成员里找显示名
        if self.service.client:
            room = self.service.client.rooms.get(self.current_room_id)
            if room:
                return room.user_name(sender_id) or sender_id
        return sender_id

    def _on_input_changed(self):
        has_text = bool(self.input.toPlainText().strip())
        self.send_btn.setEnabled(has_text and bool(self.current_room_id))

    def _on_receipt(self, event_id, user_id):
        widget = self._sent_widgets.get(event_id)
        if widget and getattr(widget, "status_label", None):
            widget.set_status("已读")

    def _on_mention(self, room_id, sender_name, body):
        self.user_sub.setText(f"🔔 @提及 · {sender_name}：{body[:30]}")
        self.user_sub.setStyleSheet("color: #f0b232; font-size: 11px;")
        try:
            self.tray.showMessage(f"@{sender_name} 提及了你", body, QSystemTrayIcon.MessageIcon.Warning, 5000)
            QApplication.beep()
        except Exception:  # noqa: BLE001
            pass

    def _on_typing(self, room_id, user_ids):
        if room_id != self.current_room_id or not user_ids:
            return
        name = self._display_name(user_ids[0])
        self.chat_sub.setText(f"{name} 正在输入……")
        QTimer.singleShot(1500, self._restore_chat_sub)

    def _restore_chat_sub(self):
        if not self.current_room_id:
            return
        for r in self.service.rooms():
            if r["room_id"] == self.current_room_id:
                self.chat_sub.setText(f"{r['member_count']} 名成员" + (" · 私聊" if r["is_dm"] else " · 群聊"))
                break

    def _on_status(self, text):
        if "中断" in text or "重试" in text:
            self.user_sub.setText(f"⚠️ 连接中断，自动重连中……")
            self.user_sub.setStyleSheet("color: #f0b232; font-size: 11px;")
        else:
            self.user_sub.setText(f"🟢 已连接 · {text}")
            self.user_sub.setStyleSheet("color: #3ba55d; font-size: 11px;")

    async def _set_room_avatar(self, item, user_id):
        data = await self.service.get_avatar_data(user_id)
        if data:
            pixmap = QPixmap()
            if pixmap.loadFromData(data):
                scaled = pixmap.scaled(28, 28, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
                item.setIcon(QIcon(scaled))

    async def _set_group_avatar(self, item, room_id):
        # 群聊：取前 2 个成员头像合成堆叠图标
        if not self.service.client:
            return
        room = self.service.client.rooms.get(room_id)
        if not room:
            return
        users = list(room.users.keys())[:2]
        pixmaps = []
        for uid in users:
            data = await self.service.get_avatar_data(uid)
            if data:
                pm = QPixmap()
                if pm.loadFromData(data):
                    pixmaps.append(pm.scaled(20, 20, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation))
        if pixmaps:
            combined = QPixmap(34, 20)
            combined.fill(Qt.GlobalColor.transparent)
            painter = QPainter(combined)
            for i, pm in enumerate(pixmaps[:2]):
                painter.drawPixmap(i * 14, 0, pm)
            painter.end()
            item.setIcon(QIcon(combined))

    def _room_name(self, room_id: str) -> str:
        for r in self.service.rooms():
            if r["room_id"] == room_id:
                return r["display_name"]
        return room_id

    def _on_tray_activated(self, reason):
        # 单击/双击托盘图标时恢复窗口
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def _on_notify_clicked(self):
        # 点击系统通知 → 切换到对应房间
        if self._notify_room:
            self._switch_to_room(self._notify_room)

    def _switch_to_room(self, room_id):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if room_id != self.current_room_id:
            self.current_room_id = room_id
            asyncio.create_task(self._load_room(room_id))
        for lst in (self.group_list, self.dm_list):
            for i in range(lst.count()):
                item = lst.item(i)
                if item.data(Qt.ItemDataRole.UserRole) == room_id:
                    lst.setCurrentItem(item)
                    break

    def _on_key_received(self, room_id):
        # 密钥到达，刷新当前房间（防抖，避免批量密钥频繁重载）
        if room_id != self.current_room_id:
            return
        if getattr(self, "_key_refresh_pending", False):
            return
        self._key_refresh_pending = True

        def do_refresh():
            self._key_refresh_pending = False
            asyncio.create_task(self._load_room(room_id))

        QTimer.singleShot(2000, do_refresh)

    # ---------------- 交互 ----------------
    def _send(self):
        if not self.current_room_id:
            return
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        asyncio.create_task(self._do_send(text))

    def _on_image_pasted(self, qimage):
        if not self.current_room_id:
            return
        path = os.path.join(tempfile.gettempdir(), f"mchat_paste_{datetime.now().strftime('%H%M%S%f')}.png")
        qimage.save(path)
        asyncio.create_task(self._do_send_image(path))

    _EMOJIS = ["😀", "😂", "😊", "❤️", "👍", "😮", "😢", "😡", "🔥", "🎉", "✅", "❌", "🤔", "😴", "🙏", "💯", "🎂", "🚀", "✨", "👀"]

    def _show_emoji_menu(self):
        menu = QMenu(self)
        for emoji in self._EMOJIS:
            menu.addAction(emoji)
        chosen = menu.exec(self.emoji_btn.mapToGlobal(self.emoji_btn.rect().bottomLeft()))
        if chosen:
            self._insert_emoji(chosen.text())

    def _insert_emoji(self, emoji):
        cursor = self.input.textCursor()
        cursor.insertText(emoji)
        self.input.setFocus()

    def _send_image(self):
        if not self.current_room_id:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择图片",
            "",
            "图片 (*.png *.jpg *.jpeg *.gif *.webp *.bmp)",
        )
        if not path:
            return
        asyncio.create_task(self._do_send_image(path))

    async def _do_send_image(self, path):
        try:
            await self.service.send_image(self.current_room_id, path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "图片发送失败", str(exc))

    async def _do_send(self, text):
        try:
            reply_to = self._reply_to[0] if self._reply_to else None
            event_id = await self.service.send_text(self.current_room_id, text, reply_to=reply_to)
            self._cancel_reply()
            # 乐观回显：立即显示自己发的消息（sync 回调因 event_id 去重而跳过）
            widget = self._append_message(
                event_id,
                self.service.client.user_id if self.service.client else "",
                text,
                int(datetime.now().timestamp() * 1000),
                True,
            )
            if widget:
                widget.set_status("发送中")
                self._pending_widgets[event_id] = widget
        except Exception as exc:  # noqa: BLE001
            self.input.setPlainText(text)  # 恢复输入框，方便直接重试
            QMessageBox.warning(self, "发送失败", str(exc))

    def _show_new_menu(self):
        menu = QMenu(self)
        act_group = menu.addAction("创建群聊房间")
        act_dm = menu.addAction("发起私聊")
        chosen = menu.exec(self.add_btn.mapToGlobal(self.add_btn.rect().bottomLeft()))
        if chosen == act_group:
            self._create_group()
        elif chosen == act_dm:
            self._create_dm()

    def _create_group(self):
        name, ok = QInputDialog.getText(self, "创建群聊", "房间名称：")
        if not ok or not name.strip():
            return
        asyncio.create_task(self._do_create_group(name.strip()))

    async def _do_create_group(self, name):
        try:
            room_id = await self.service.create_group_room(name)
            self.current_room_id = room_id
            self._refresh_rooms()
            await self._load_room(room_id)
            # 提示邀请好友
            if QMessageBox.question(self, "房间已创建", "群聊已创建。是否现在邀请好友？") == QMessageBox.StandardButton.Yes:
                self._invite()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "创建失败", str(exc))

    def _create_dm(self):
        uid, ok = QInputDialog.getText(self, "发起私聊", "好友的 Matrix ID（如 @friend:matrix.org）：")
        if not ok or not uid.strip():
            return
        asyncio.create_task(self._do_create_dm(uid.strip()))

    async def _do_create_dm(self, uid):
        try:
            room_id = await self.service.create_dm(uid)
            self.current_room_id = room_id
            self._refresh_rooms()
            await self._load_room(room_id)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "私聊失败", str(exc))

    def _join_room(self):
        rid, ok = QInputDialog.getText(self, "加入房间", "房间 ID 或邀请链接（!xxx:matrix.org）：")
        if not ok or not rid.strip():
            return
        rid = rid.strip()
        # 从邀请链接提取房间 ID
        if "/" in rid:
            rid = rid.split("/")[-1].split("?")[0]
        asyncio.create_task(self._do_join_room(rid))

    async def _do_join_room(self, rid):
        try:
            await self.service.join_room(rid)
            self.current_room_id = rid
            self._refresh_rooms()
            await self._load_room(rid)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "加入失败", str(exc))

    def _show_members(self):
        if not self.current_room_id or not self.service.client:
            return
        room = self.service.client.rooms.get(self.current_room_id)
        if not room:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("房间成员")
        dlg.setMinimumSize(340, 420)
        lay = QVBoxLayout(dlg)
        lst = QListWidget()
        users = getattr(room, "users", {})
        for uid in users:
            name = room.user_name(uid) or uid
            is_self = uid == self.service.client.user_id
            suffix = "（我）" if is_self else ""
            lst.addItem(f"{name}{suffix}\n  {uid}")
        lay.addWidget(lst)
        dlg.exec()

    def _search_message(self):
        if not self.current_room_id:
            return
        keyword, ok = QInputDialog.getText(self, "搜索消息", "输入关键词：")
        if not ok or not keyword.strip():
            return
        keyword = keyword.strip().lower()
        for i in range(self.msg_list.count()):
            item = self.msg_list.item(i)
            body = item.data(Qt.ItemDataRole.UserRole)
            if body and keyword in body.lower():
                self.msg_list.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)
                self.msg_list.setCurrentItem(item)
                return
        QMessageBox.information(self, "搜索", "未找到匹配的消息")

    def _invite(self):
        if not self.current_room_id:
            return
        uid, ok = QInputDialog.getText(self, "邀请好友", "好友的 Matrix ID（如 @friend:matrix.org）：")
        if not ok or not uid.strip():
            return
        asyncio.create_task(self._do_invite(uid.strip()))

    async def _do_invite(self, uid):
        try:
            await self.service.invite(self.current_room_id, uid)
            QMessageBox.information(self, "已邀请", f"已向 {uid} 发送邀请")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "邀请失败", str(exc))

    # ---------------- 更新 ----------------
    def _on_update_found(self, info):
        if not info or not is_newer(info.tag_name, __version__):
            return
        self.chat_sub.setText(f"有新版本 {info.tag_name} 可用")
        self.chat_sub.setStyleSheet("color: #f0b232; font-size: 12px;")
        setup_asset = self._find_setup_asset(info)
        box = QMessageBox(self)
        box.setWindowTitle("发现新版本")
        box.setText(f"当前版本 {__version__}，最新版本 {info.tag_name}。\n\n{info.body[:200]}")
        dl_btn = None
        if setup_asset:
            dl_btn = box.addButton("下载安装包", QMessageBox.ButtonRole.AcceptRole)
            open_btn = box.addButton("打开下载页", QMessageBox.ButtonRole.ActionRole)
        else:
            open_btn = box.addButton("打开下载页", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("稍后", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if dl_btn is not None and clicked == dl_btn:
            self._download_update(setup_asset)
        elif clicked == open_btn:
            import webbrowser
            webbrowser.open(info.html_url)

    @staticmethod
    def _find_setup_asset(info):
        for asset in info.assets:
            name = (asset.get("name") or "").lower()
            if name.endswith(".exe") and "setup" in name:
                return asset
        return None

    def _download_update(self, asset):
        url = asset.get("browser_download_url")
        if not url:
            return
        name = asset.get("name") or "MChat-Setup.exe"
        dest = os.path.join(tempfile.gettempdir(), name)
        self.chat_sub.setText(f"正在下载 {name} ……")

        def worker():
            try:
                download_asset(url, dest)
                QTimer.singleShot(0, lambda: self._on_download_done(dest))
            except Exception as exc:  # noqa: BLE001
                QTimer.singleShot(0, lambda: self._on_download_failed(str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_download_done(self, dest):
        self.chat_sub.setText("新版本已下载完成")
        if QMessageBox.question(
            self, "下载完成", f"安装包已下载到：\n{dest}\n\n是否立即运行安装程序？",
        ) == QMessageBox.StandardButton.Yes:
            subprocess.Popen([dest])

    def _on_download_failed(self, msg):
        self.chat_sub.setText("下载失败")
        QMessageBox.warning(self, "下载失败", msg)

    def _logout(self):
        if QMessageBox.question(self, "登出", "确定要登出吗？本地凭证将被清除。") == QMessageBox.StandardButton.Yes:
            async def do_logout():
                await self.service.logout()
                QApplication.quit()
            asyncio.create_task(do_logout())

    def closeEvent(self, e):
        if self.service.client:
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
