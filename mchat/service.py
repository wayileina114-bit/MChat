"""Matrix 通信服务：登录、收发消息、拉取历史（基于 matrix-nio，端到端加密）。

本模块不依赖 Qt，只依赖 asyncio 和 matrix-nio，方便单独测试。
GUI 通过回调（on_message / on_status）接收事件。
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
    RoomMessageText,
    RoomMessagesError,
    RoomSendError,
    RoomVisibility,
)
from nio.store import SqliteStore

from .config import ROOT, account, load_config, save_config

logging.getLogger("nio").setLevel(logging.CRITICAL)

_LOCAL_STORE_PASSPHRASE = "mchat-local-store-passphrase"

# (key, label, sender_name, sender_id, body, timestamp_ms)
MessageCallback = Callable[[str, str, str, str, str, int], None]
StatusCallback = Callable[[str, str], None]


class MatrixService:
    """封装两个 Matrix 账号的登录、监听与收发。"""

    def __init__(self) -> None:
        self.clients: dict[str, AsyncClient] = {}
        self._tasks: list[asyncio.Task] = []
        self.on_message: Optional[MessageCallback] = None
        self.on_status: Optional[StatusCallback] = None
        self.room_id: str = load_config().get("room_id", "")

    # ---- 客户端构建 ----
    def make_client(self, key: str) -> AsyncClient:
        cfg = load_config()
        acct = account(cfg, key)
        config = AsyncClientConfig(
            encryption_enabled=True,
            store=SqliteStore,
            store_name="nio.db",
            store_sync_tokens=False,
            pickle_key=_LOCAL_STORE_PASSPHRASE,
        )
        store_path = ROOT / acct["store_dir"]
        store_path.mkdir(parents=True, exist_ok=True)
        return AsyncClient(
            homeserver=cfg["homeserver"],
            user=acct["user_id"],
            device_id=acct["device_id"],
            store_path=str(store_path),
            config=config,
            proxy=cfg.get("proxy") or None,
        )

    # ---- 登录 ----
    async def login(self, key: str) -> AsyncClient:
        cfg = load_config()
        acct = account(cfg, key)
        client = self.make_client(key)

        if acct.get("access_token"):
            client.restore_login(
                acct["user_id"], acct["device_id"], acct["access_token"]
            )
        else:
            resp = await client.login(
                password=acct["password"], device_name=acct["device_id"]
            )
            if isinstance(resp, LoginError):
                raise RuntimeError(
                    f"[{acct['label']}] 登录失败（HTTP {resp.status_code}）：{resp.message}"
                )
            acct["access_token"] = client.access_token
            save_config(cfg)

        if client.should_upload_keys:
            await client.keys_upload()
        self.clients[key] = client
        self._report(key, "已登录")
        return client

    # ---- 连接并开始后台同步 ----
    async def connect(self, key: str) -> AsyncClient:
        client = self.clients.get(key)
        if client is None:
            client = await self.login(key)

        # 先同步一次，把房间/成员状态加载到本地（否则立刻发消息会报 No such room）
        await client.sync(timeout=3000)

        label = account(load_config(), key)["label"]

        def on_event(room, event, _key=key, _label=label):
            self._handle_event(_key, _label, room, event)

        def on_invite(room, event, _client=client, _key=key):
            asyncio.create_task(self._handle_invite(_client, _key, room, event))

        client.add_event_callback(on_event, (RoomMessageText, MegolmEvent))
        client.add_event_callback(on_invite, InviteMemberEvent)

        task = asyncio.create_task(self._sync_loop(client, key))
        self._tasks.append(task)
        self._report(key, "开始监听")
        return client

    async def _sync_loop(self, client: AsyncClient, key: str) -> None:
        try:
            await client.sync_forever(timeout=30_000, loop_sleep_time=500)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._report(key, f"同步中断：{exc}")

    def _handle_event(self, key: str, label: str, room, event) -> None:
        if isinstance(event, RoomMessageText):
            sender_name = room.user_name(event.sender) or event.sender
            if self.on_message:
                self.on_message(
                    key,
                    label,
                    sender_name,
                    event.sender,
                    event.body,
                    event.server_timestamp,
                )
        # MegolmEvent（尚未解密）忽略：解密成功后 nio 会再以 RoomMessageText 回调。

    async def _handle_invite(self, client: AsyncClient, key: str, room, event) -> None:
        try:
            if room.room_id in client.invited_rooms:
                await client.join(room.room_id)
                self._report(key, f"已接受邀请并加入 {room.room_id}")
        except Exception as exc:  # noqa: BLE001
            self._report(key, f"加入房间失败：{exc}")

    def _report(self, key: str, text: str) -> None:
        if self.on_status:
            self.on_status(key, text)

    # ---- 发送 ----
    async def send_text(self, key: str, text: str) -> None:
        client = self.clients.get(key)
        if client is None:
            raise RuntimeError("尚未连接")
        room_id = self.room_id or load_config().get("room_id")
        if not room_id:
            raise RuntimeError("还没有房间，请先完成初始化")
        resp = await client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": text},
            ignore_unverified_devices=True,
        )
        if isinstance(resp, RoomSendError):
            raise RuntimeError(f"发送失败（HTTP {resp.status_code}）：{resp.message}")

    # ---- 历史消息 ----
    async def history(self, key: str, limit: int = 50):
        client = self.clients.get(key)
        if client is None:
            return []
        room_id = self.room_id or load_config().get("room_id")
        if not room_id:
            return []
        resp = await client.room_messages(room_id, start=None, limit=limit)
        if isinstance(resp, RoomMessagesError):
            return []
        out = []
        for ev in resp.chunk:
            if isinstance(ev, MegolmEvent):
                try:
                    dec = await client.decrypt_event(ev)
                    if isinstance(dec, RoomMessageText):
                        out.append(dec)
                except Exception:  # noqa: BLE001
                    pass
            elif isinstance(ev, RoomMessageText):
                out.append(ev)
        return out

    # ---- 首次初始化：建加密房间 + 握手 ----
    async def setup_room(self) -> str:
        cfg = load_config()
        room_id = cfg.get("room_id")
        a = self.clients.get("a") or await self.login("a")
        b = self.clients.get("b") or await self.login("b")

        if not room_id:
            resp = await a.room_create(
                visibility=RoomVisibility.private,
                name=cfg.get("room_name", "MChat 通道"),
                invite=[b.user_id],
                initial_state=[
                    {
                        "type": "m.room.encryption",
                        "state_key": "",
                        "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                    }
                ],
            )
            if isinstance(resp, RoomCreateError):
                raise RuntimeError(
                    f"创建房间失败（HTTP {resp.status_code}）：{resp.message}"
                )
            room_id = resp.room_id
            cfg["room_id"] = room_id
            save_config(cfg)

        for _ in range(3):
            await a.sync(timeout=2000)
            await b.sync(timeout=2000)

        if room_id not in b.rooms:
            resp = await b.join(room_id)
            if isinstance(resp, JoinError):
                raise RuntimeError(
                    f"程序B 加入失败（HTTP {resp.status_code}）：{resp.message}"
                )
            await a.sync(timeout=2000)
            await b.sync(timeout=2000)

        await self.send_text("a", "握手：我是程序A")
        for _ in range(5):
            await b.sync(timeout=2000)
        await self.send_text("b", "握手：我是程序B")
        for _ in range(5):
            await a.sync(timeout=2000)

        self.room_id = room_id
        return room_id

    # ---- 关闭 ----
    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for c in self.clients.values():
            await c.close()
        self.clients.clear()
        self._tasks.clear()
