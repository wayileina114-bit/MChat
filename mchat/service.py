"""Matrix 通信服务：单账号登录、房间管理、收发消息（matrix-nio，端到端加密）。

不依赖 Qt，只依赖 asyncio 和 matrix-nio。
GUI 通过回调（on_message / on_room_update / on_status）接收事件。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteMemberEvent,
    JoinError,
    LoginError,
    MegolmEvent,
    RoomCreateError,
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

# (room_id, event_id, sender_name, sender_id, body, timestamp_ms)
MessageCallback = Callable[[str, str, str, str, str, int], None]
RoomCallback = Callable[[], None]
StatusCallback = Callable[[str], None]


class MatrixService:
    """封装单个 Matrix 账号的登录、房间列表与收发。"""

    def __init__(self) -> None:
        self.client: Optional[AsyncClient] = None
        self._task: Optional[asyncio.Task] = None
        self.on_message: Optional[MessageCallback] = None
        self.on_room_update: Optional[RoomCallback] = None
        self.on_status: Optional[StatusCallback] = None

    # ---------------- 客户端与登录 ----------------
    def make_client(self) -> AsyncClient:
        cfg = load_config()
        config = AsyncClientConfig(
            encryption_enabled=True,
            store=SqliteStore,
            store_name="nio.db",
            store_sync_tokens=True,
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

        # 首次同步，加载房间与成员状态
        await client.sync(timeout=3000)
        self._task = asyncio.create_task(self._sync_loop(client))
        self._report("开始监听")
        return client

    async def _sync_loop(self, client: AsyncClient) -> None:
        try:
            await client.sync_forever(timeout=30_000, loop_sleep_time=500)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._report(f"同步中断：{exc}")

    def _handle_event(self, room, event) -> None:
        if isinstance(event, RoomMessageText):
            self._emit(
                room.room_id,
                event.event_id,
                room.user_name(event.sender) or event.sender,
                event.sender,
                event.body,
                event.server_timestamp,
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

    def _emit(self, room_id, event_id, sender_name, sender_id, body, ts) -> None:
        if self.on_message:
            self.on_message(room_id, event_id, sender_name, sender_id, body, ts)

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
                }
            )
        result.sort(key=lambda r: (r["is_dm"], r["display_name"].lower()))
        return result

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
        resp = await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": text},
            ignore_unverified_devices=True,
        )
        if isinstance(resp, RoomSendError):
            raise RuntimeError(f"发送失败（HTTP {resp.status_code}）：{resp.message}")
        return resp.event_id

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
