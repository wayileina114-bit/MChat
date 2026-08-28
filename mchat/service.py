"""Matrix 通信服务：单账号登录、房间管理、收发消息（matrix-nio，端到端加密）。

不依赖 Qt，只依赖 asyncio 和 matrix-nio。
GUI 通过回调（on_message / on_room_update / on_status）接收事件。
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
from pathlib import Path
from typing import Callable, Optional

from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteMemberEvent,
    JoinError,
    LoginError,
    MegolmEvent,
    ForwardedRoomKeyEvent,
    RoomKeyEvent,
    RoomCreateError,
    RoomMessageImage,
    RoomEncryptedImage,
    UploadError,
    DownloadError,
    RoomInviteError,
    RoomMessageText,
    RoomMessagesError,
    RoomSendError,
    RoomVisibility,
)
from nio.store import SqliteStore

from .config import ROOT, load_config, save_config

logging.getLogger("nio").setLevel(logging.CRITICAL)

_LOCAL_STORE_PASSPHRASE = "mchat-local-store-passphrase"

# 无法解密消息的占位文本
PLACEHOLDER = "🔒 无法解密的消息（缺少密钥）"

# (room_id, event_id, sender_name, sender_id, body, timestamp_ms, image_url)
MessageCallback = Callable[[str, str, str, str, str, int, "str | None"], None]
RoomCallback = Callable[[], None]
StatusCallback = Callable[[str], None]
KeyCallback = Callable[[str], None]  # (room_id)


class MatrixService:
    """封装单个 Matrix 账号的登录、房间列表与收发。"""

    def __init__(self) -> None:
        self.client: Optional[AsyncClient] = None
        self._task: Optional[asyncio.Task] = None
        self.on_message: Optional[MessageCallback] = None
        self.on_room_update: Optional[RoomCallback] = None
        self.on_status: Optional[StatusCallback] = None
        self.on_key_received: Optional[KeyCallback] = None
        self.unread: dict[str, int] = {}
        self._session_start_ms: int = 0
        self.last_messages: dict[str, tuple[str, str]] = {}  # room_id -> (sender_name, body)

    # ---------------- 客户端与登录 ----------------
    def make_client(self) -> AsyncClient:
        cfg = load_config()
        config = AsyncClientConfig(
            encryption_enabled=True,
            store=SqliteStore,
            store_name="nio.db",
            store_sync_tokens=False,  # 必须 False：nio 不持久化房间列表，False 保证每次启动全量同步加载房间
            pickle_key=_LOCAL_STORE_PASSPHRASE,
        )
        store_path = ROOT / cfg["store_dir"]
        store_path.mkdir(parents=True, exist_ok=True)
        return AsyncClient(
            homeserver=cfg["homeserver"],
            user=cfg["user_id"],
            device_id=cfg["device_id"],
            store_path=str(store_path),
            config=config,
            proxy=cfg.get("proxy") or None,
        )

    async def login(self) -> AsyncClient:
        cfg = load_config()
        client = self.make_client()
        if cfg.get("access_token"):
            client.restore_login(cfg["user_id"], cfg["device_id"], cfg["access_token"])
        else:
            resp = await client.login(password=cfg["password"], device_name=cfg["device_id"])
            if isinstance(resp, LoginError):
                raise RuntimeError(f"登录失败（HTTP {resp.status_code}）：{resp.message}")
            cfg["access_token"] = client.access_token
            save_config(cfg)
        if client.should_upload_keys:
            await client.keys_upload()
        self.client = client
        self._report("已登录")
        return client

    async def connect(self) -> AsyncClient:
        client = self.client or await self.login()

        client.add_event_callback(self._handle_event, (RoomMessageText, MegolmEvent))
        client.add_event_callback(self._handle_invite, InviteMemberEvent)
        client.add_to_device_callback(self._handle_key, (RoomKeyEvent, ForwardedRoomKeyEvent))

        # 首次同步，加载房间与成员状态
        self._session_start_ms = int(time.time() * 1000)
        await client.sync(timeout=3000)
        self._task = asyncio.create_task(self._sync_loop(client))
        self._report("开始监听")
        return client

    async def _sync_loop(self, client: AsyncClient) -> None:
        # 断线自动重连：sync 异常退出后等待片刻再重试
        while True:
            try:
                await client.sync_forever(timeout=30_000, loop_sleep_time=500)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                self._report(f"同步中断，5 秒后重试：{exc}")
                await asyncio.sleep(5)

    def _handle_event(self, room, event) -> None:
        if isinstance(event, RoomMessageText):
            # 未读计数：只统计本会话新收到的、别人发的消息（排除全量同步的历史消息）
            if (
                event.sender
                and event.sender != self.client.user_id
                and event.server_timestamp >= self._session_start_ms
            ):
                self.unread[room.room_id] = self.unread.get(room.room_id, 0) + 1
            sender_name = room.user_name(event.sender) or event.sender
            self.last_messages[room.room_id] = (sender_name, event.body)
            self._emit(
                room.room_id,
                event.event_id,
                sender_name,
                event.sender,
                event.body,
                event.server_timestamp,
            )
        elif isinstance(event, RoomMessageImage):
            sender_name = room.user_name(event.sender) or event.sender
            self._emit(
                room.room_id,
                event.event_id,
                sender_name,
                event.sender,
                f"🖼️ {event.body}",
                event.server_timestamp,
                getattr(event, "url", None),
            )
        elif isinstance(event, RoomEncryptedImage):
            sender_name = room.user_name(event.sender) or event.sender
            self._emit(
                room.room_id,
                event.event_id,
                sender_name,
                event.sender,
                f"🖼️ {event.body}",
                event.server_timestamp,
                None,
            )
        elif isinstance(event, MegolmEvent):
            # 解密失败（密钥缺失）的消息，用占位显示，避免消息凭空消失
            self._emit(
                room.room_id,
                event.event_id,
                event.sender,
                event.sender,
                PLACEHOLDER,
                event.server_timestamp,
            )

    def _emit(self, room_id, event_id, sender_name, sender_id, body, ts, image_url=None) -> None:
        if self.on_message:
            self.on_message(room_id, event_id, sender_name, sender_id, body, ts, image_url)

    def _handle_key(self, event) -> None:
        # 收到 Megolm 会话密钥：通知 GUI 刷新对应房间，让占位消息重新解密
        room_id = getattr(event, "room_id", "")
        if room_id and self.on_key_received:
            self.on_key_received(room_id)

    async def _handle_invite(self, room, event) -> None:
        try:
            if self.client and room.room_id in self.client.invited_rooms:
                await self.client.join(room.room_id)
                self._report("已接受邀请并加入房间")
                self._notify_room_update()
        except Exception as exc:  # noqa: BLE001
            self._report(f"加入房间失败：{exc}")

    # ---------------- 房间列表 ----------------
    def rooms(self) -> list:
        if not self.client:
            return []
        result = []
        for room_id, room in self.client.rooms.items():
            result.append(
                {
                    "room_id": room_id,
                    "display_name": self._room_display(room),
                    "is_dm": room.member_count == 2,
                    "member_count": room.member_count,
                    "topic": getattr(room, "topic", "") or "",
                    "unread": self.unread.get(room_id, 0),
                    "last_message": self.last_messages.get(room_id),
                }
            )
        result.sort(key=lambda r: (r["is_dm"], r["display_name"].lower()))
        return result

    def mark_read(self, room_id: str) -> None:
        self.unread[room_id] = 0

    def invited_room_ids(self) -> list:
        if not self.client:
            return []
        return list(self.client.invited_rooms.keys())

    def _room_display(self, room) -> str:
        if room.member_count == 2:
            for uid in room.users:
                if uid != self.client.user_id:
                    return room.user_name(uid) or uid
        return room.display_name or room.name or room.room_id

    # ---------------- 收发 ----------------
    async def send_text(self, room_id: str, text: str) -> str:
        """发送文本消息，返回 event_id。"""
        if not self.client:
            raise RuntimeError("尚未连接")
        # 房间尚未同步到本地时先同步一次，避免 "No such room"
        if room_id not in self.client.rooms:
            await self.client.sync(timeout=3000)
        resp = await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": text},
            ignore_unverified_devices=True,
        )
        if isinstance(resp, RoomSendError):
            raise RuntimeError(f"发送失败（HTTP {resp.status_code}）：{resp.message}")
        return resp.event_id

    async def send_image(self, room_id: str, file_path: str) -> str:
        """发送图片，返回 event_id。"""
        if not self.client:
            raise RuntimeError("尚未连接")
        path = Path(file_path)
        if not path.exists():
            raise RuntimeError(f"文件不存在：{file_path}")
        mimetype = self._guess_mime(path.suffix)
        data = path.read_bytes()
        room = self.client.rooms.get(room_id)
        encrypt = bool(room and getattr(room, "encrypted", False))
        up_resp, decryption_dict = await self.client.upload(
            io.BytesIO(data), content_type=mimetype, filename=path.name, encrypt=encrypt
        )
        if isinstance(up_resp, UploadError):
            raise RuntimeError(f"图片上传失败：{up_resp.message}")
        content = {
            "msgtype": "m.image",
            "body": path.name,
            "info": {"mimetype": mimetype, "size": len(data)},
        }
        if encrypt and decryption_dict:
            # 加密媒体：把解密信息（key/iv/hashes）放进 file 字段
            file_info = dict(decryption_dict)
            file_info["url"] = up_resp.content_uri
            content["file"] = file_info
        else:
            content["url"] = up_resp.content_uri
        resp = await self.client.room_send(
            room_id, "m.room.message", content, ignore_unverified_devices=True
        )
        if isinstance(resp, RoomSendError):
            raise RuntimeError(f"发送失败（HTTP {resp.status_code}）：{resp.message}")
        return resp.event_id

    async def download_media(self, mxc_url: str):
        """下载媒体（图片等），返回 bytes；失败返回 None。"""
        if not self.client:
            return None
        try:
            resp = await self.client.download(mxc_url)
        except Exception:  # noqa: BLE001
            return None
        if isinstance(resp, DownloadError):
            return None
        return getattr(resp, "body", None)

    @staticmethod
    def _guess_mime(suffix: str) -> str:
        return {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }.get(suffix.lower(), "image/png")

    async def history(self, room_id: str, limit: int = 100):
        """返回房间历史（旧→新），解密失败的消息用占位文本代替，不丢弃。"""
        if not self.client:
            return []
        resp = await self.client.room_messages(room_id, start=None, limit=limit)
        if isinstance(resp, RoomMessagesError):
            return []
        out = []
        for ev in resp.chunk:
            if isinstance(ev, MegolmEvent):
                try:
                    dec = await self.client.decrypt_event(ev)
                    if isinstance(dec, RoomMessageText):
                        out.append(
                            {
                                "event_id": dec.event_id,
                                "sender": dec.sender,
                                "body": dec.body,
                                "ts": dec.server_timestamp,
                                "decrypted": True,
                            }
                        )
                        continue
                    if isinstance(dec, RoomMessageImage):
                        out.append(
                            {
                                "event_id": dec.event_id,
                                "sender": dec.sender,
                                "body": f"🖼️ {dec.body}",
                                "ts": dec.server_timestamp,
                                "decrypted": True,
                                "image_url": getattr(dec, "url", None),
                            }
                        )
                        continue
                except Exception:  # noqa: BLE001
                    pass
                # 解密失败：保留占位，不丢弃
                out.append(
                    {
                        "event_id": ev.event_id,
                        "sender": ev.sender,
                        "body": PLACEHOLDER,
                        "ts": ev.server_timestamp,
                        "decrypted": False,
                    }
                )
            elif isinstance(ev, RoomMessageText):
                out.append(
                    {
                        "event_id": ev.event_id,
                        "sender": ev.sender,
                        "body": ev.body,
                        "ts": ev.server_timestamp,
                        "decrypted": True,
                    }
                )
            elif isinstance(ev, RoomMessageImage):
                out.append(
                    {
                        "event_id": ev.event_id,
                        "sender": ev.sender,
                        "body": f"🖼️ {ev.body}",
                        "ts": ev.server_timestamp,
                        "decrypted": True,
                        "image_url": getattr(ev, "url", None),
                    }
                )
            elif isinstance(ev, RoomEncryptedImage):
                out.append(
                    {
                        "event_id": ev.event_id,
                        "sender": ev.sender,
                        "body": f"🖼️ {ev.body}",
                        "ts": ev.server_timestamp,
                        "decrypted": True,
                        "image_url": None,
                    }
                )
        return list(reversed(out))  # 旧 → 新

    # ---------------- 房间操作 ----------------
    async def create_group_room(self, name: str, invite_ids: list | None = None) -> str:
        if not self.client:
            raise RuntimeError("尚未连接")
        resp = await self.client.room_create(
            visibility=RoomVisibility.private,
            name=name,
            invite=invite_ids or [],
            initial_state=[
                {
                    "type": "m.room.encryption",
                    "state_key": "",
                    "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                }
            ],
        )
        if isinstance(resp, RoomCreateError):
            raise RuntimeError(f"创建房间失败：{resp.message}")
        await self.client.sync(timeout=3000)
        self._notify_room_update()
        return resp.room_id

    async def create_dm(self, user_id: str) -> str:
        if not self.client:
            raise RuntimeError("尚未连接")
        resp = await self.client.room_create(
            visibility=RoomVisibility.private,
            is_direct=True,
            invite=[user_id],
            initial_state=[
                {
                    "type": "m.room.encryption",
                    "state_key": "",
                    "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                }
            ],
        )
        if isinstance(resp, RoomCreateError):
            raise RuntimeError(f"创建私聊失败：{resp.message}")
        await self.client.sync(timeout=3000)
        self._notify_room_update()
        return resp.room_id

    async def invite(self, room_id: str, user_id: str) -> None:
        resp = await self.client.room_invite(room_id, user_id)
        if isinstance(resp, RoomInviteError):
            raise RuntimeError(f"邀请失败：{resp.message}")

    async def join_room(self, room_id: str) -> str:
        resp = await self.client.join(room_id)
        if isinstance(resp, JoinError):
            raise RuntimeError(f"加入失败（HTTP {resp.status_code}）：{resp.message}")
        await self.client.sync(timeout=3000)
        self._notify_room_update()
        return room_id

    async def leave_room(self, room_id: str) -> None:
        await self.client.room_leave(room_id)
        self._notify_room_update()

    async def logout(self) -> None:
        """登出：撤销服务器 token，清除本地凭证。"""
        cfg = load_config()
        if self.client and cfg.get("access_token"):
            try:
                await self.client.logout()
            except Exception:  # noqa: BLE001
                pass
        cfg["access_token"] = ""
        cfg["password"] = ""
        save_config(cfg)
        await self.close()

    # ---------------- 工具 ----------------
    def _report(self, text: str) -> None:
        if self.on_status:
            self.on_status(text)

    def _notify_room_update(self) -> None:
        if self.on_room_update:
            self.on_room_update()

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self.client:
            await self.client.close()
            self.client = None
