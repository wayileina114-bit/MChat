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
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
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
    def __init__(self, sender_name: str, sender_id: str, body: str, ts_ms: int, parent=None, is_placeholder: bool = False, image_url: str | None = None):
        super().__init__(parent)
        self.image_label = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(10)

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
        name_lbl.setStyleSheet(f"color: {name_color(sender_id)}; font-weight: bold; font-size: 14px;")
        time_lbl = QLabel(fmt_time(ts_ms))
        time_lbl.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 11px;")
        try:
            time_lbl.setToolTip(datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:  # noqa: BLE001
            pass
        head.addWidget(name_lbl)
        head.addWidget(time_lbl)
        head.addStretch(1)
        col.addLayout(head)

        if image_url:
            self.image_label = QLabel("🖼️ 图片加载中……")
            self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.image_label.setMinimumSize(160, 100)
            self.image_label.setStyleSheet(
                f"background: {C_INPUT}; border-radius: 8px; color: {C_TEXT_MUTED};"
            )
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

    def set_image(self, data: bytes):
        label = getattr(self, "image_label", None)
        if not label or not data:
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            label.setText("🖼️ 图片无法显示")
            return
        scaled = pixmap.scaledToWidth(260, Qt.TransformationMode.SmoothTransformation)
        label.setPixmap(scaled)
        label.setFixedSize(scaled.size())
        label.setStyleSheet("")


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
        self.current_room_id = ""
        self._seen_events: set = set()
        self._last_msg_date: str | None = None
        self._started = False

        self.setWindowTitle(f"{__app_name__} v{__version__}")
        self.resize(1050, 700)

        service.on_message = self._on_message
        service.on_room_update = self._refresh_rooms
        service.on_status = self._on_status
        service.on_key_received = self._on_key_received

        self._build_ui()

        # 系统托盘：用于新消息通知
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation))
        self.tray.setToolTip(f"{__app_name__} v{__version__}")
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

        self.updater = UpdateChecker()
        self.updater.found.connect(self._on_update_found)

        # 定期刷新房间列表
        self._room_timer = QTimer(self)
        self._room_timer.timeout.connect(self._refresh_rooms)
        self._room_timer.start(5000)

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
        group_lbl = QLabel("群聊")
        group_lbl.setObjectName("sectionLabel")
        side_lay.addWidget(group_lbl)
        self.group_list = QListWidget()
        self.group_list.setObjectName("roomList")
        self.group_list.itemClicked.connect(self._on_room_clicked)
        side_lay.addWidget(self.group_list, 1)

        dm_lbl = QLabel("私聊")
        dm_lbl.setObjectName("sectionLabel")
        side_lay.addWidget(dm_lbl)
        self.dm_list = QListWidget()
        self.dm_list.setObjectName("roomList")
        self.dm_list.itemClicked.connect(self._on_room_clicked)
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
        chat_lay.addWidget(self.msg_list, 1)

        input_bar = QFrame()
        input_bar.setObjectName("inputBar")
        input_lay = QHBoxLayout(input_bar)
        input_lay.setContentsMargins(16, 12, 16, 12)
        input_lay.setSpacing(10)
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
        self.input.setFixedHeight(64)
        self.input.setEnabled(False)
        self.input.submitted.connect(self._send)
        self.input.textChanged.connect(self._on_input_changed)
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
            self.user_sub.setText("登录失败，请检查配置")
            return

        self.user_sub.setText("已连接 · 端到端加密已启用")
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
        for r in rooms:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, r["room_id"])
            item.setSizeHint(QSize(0, 40))
            icon = "#" if not r["is_dm"] else "@"
            name = r["display_name"]
            unread = r.get("unread", 0)
            unread_txt = f"  ({unread})" if unread else ""
            lm = r.get("last_message")
            if lm:
                sender, preview = lm
                preview = preview.replace("\n", " ").strip()
                if len(preview) > 18:
                    preview = preview[:18] + "…"
                item.setText(f"{icon} {name}{unread_txt}\n  {sender}: {preview}")
            else:
                item.setText(f"{icon} {name}{unread_txt}")
            if r["room_id"] == self.current_room_id:
                item.setSelected(True)
            target = self.dm_list if r["is_dm"] else self.group_list
            target.addItem(item)

    def _on_room_clicked(self, item):
        room_id = item.data(Qt.ItemDataRole.UserRole)
        if room_id != self.current_room_id:
            self.current_room_id = room_id
            asyncio.create_task(self._load_room(room_id))

    async def _load_room(self, room_id):
        self.msg_list.clear()
        self._seen_events.clear()
        self._last_msg_date = None
        self.service.mark_read(room_id)
        self._refresh_rooms()
        # 更新标题
        for r in self.service.rooms():
            if r["room_id"] == room_id:
                self.chat_title.setText(r["display_name"])
                self.chat_sub.setText(f"{r['member_count']} 名成员" + (" · 私聊" if r["is_dm"] else " · 群聊"))
                break
        self.input.setEnabled(True)
        self.send_btn.setEnabled(True)
        self.invite_btn.setEnabled(True)
        self.image_btn.setEnabled(True)
        try:
            msgs = await self.service.history(room_id, limit=100)
            for m in msgs:
                self._append_message(
                    m["event_id"], m["sender"], m["body"], m["ts"], m["decrypted"], m.get("image_url")
                )
        except Exception:  # noqa: BLE001
            pass
        self.msg_list.scrollToBottom()

    # ---------------- 消息 ----------------
    def _on_message(self, room_id, event_id, sender_name, sender_id, body, ts_ms, image_url=None):
        if room_id == self.current_room_id:
            self._append_message(event_id, sender_id, body, ts_ms, True, image_url)
        else:
            # 非当前房间的新消息：系统托盘通知（过滤自己发 + 历史同步）
            if (
                self.service.client
                and sender_id != self.service.client.user_id
                and ts_ms >= self.service._session_start_ms
                and body != "🔒 无法解密的消息（缺少密钥）"
            ):
                room_name = self._room_name(room_id)
                self.tray.showMessage(
                    f"{sender_name} · {room_name}",
                    body,
                    QSystemTrayIcon.MessageIcon.Information,
                    5000,
                )

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
        self.msg_list.addItem(item)
        widget = MessageWidget(name, sender_id, body, ts_ms, is_placeholder=not decrypted, image_url=image_url)
        self.msg_list.setItemWidget(item, widget)
        if image_url:
            asyncio.create_task(self._load_image(widget, image_url))
        while self.msg_list.count() > 500:
            self.msg_list.takeItem(0)
        self.msg_list.scrollToBottom()

    async def _load_image(self, widget, image_url):
        data = await self.service.download_media(image_url)
        if data:
            widget.set_image(data)

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
        menu = QMenu(self)
        act_copy = menu.addAction("复制消息")
        chosen = menu.exec(self.msg_list.viewport().mapToGlobal(pos))
        if chosen == act_copy:
            QApplication.clipboard().setText(body)

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

    def _on_status(self, text):
        self.user_sub.setText(f"已连接 · {text}")

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
            event_id = await self.service.send_text(self.current_room_id, text)
            # 乐观回显：立即显示自己发的消息（sync 回调因 event_id 去重而跳过）
            self._append_message(
                event_id,
                self.service.client.user_id if self.service.client else "",
                text,
                int(datetime.now().timestamp() * 1000),
                True,
            )
        except Exception as exc:  # noqa: BLE001
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
