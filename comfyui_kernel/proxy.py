"""Persistent WebSocket client from the kernel process to ComfyUI.

Runs on a daemon thread hosting its own asyncio loop. Methods are designed
to be safe to call from any thread:
  - `call_sync(op, **fields)` blocks the caller until a `<op>_reply`
    arrives. Used for execute / complete request/reply cycles.
  - `send(payload)` is fire-and-forget — used to forward comm_* messages
    coming in from the frontend, where no reply is expected.
The bridge re-connects forever on failure; sends issued while disconnected
queue briefly (best-effort) and raise after a short timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import uuid
from typing import Any, Callable

import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

IopubHandler = Callable[[dict[str, Any]], None]


def _json_default(obj: Any) -> str:
    # Headers contain datetime; ISO-8601 round-trips through ComfyUI fine.
    return obj.isoformat() if hasattr(obj, "isoformat") else str(obj)


class KernelProxy:
    def __init__(
        self,
        url: str,
        *,
        on_iopub: IopubHandler,
        reconnect_delay: float = 2.0,
    ) -> None:
        self._url = url
        self._on_iopub = on_iopub
        self._reconnect_delay = reconnect_delay
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: ClientConnection | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._loop_ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="comfyui-kernel-proxy",
            daemon=True,
        )
        self._thread.start()
        self._loop_ready.wait()

    # --- thread-side ---

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.create_task(self._connect_forever())
        self._loop_ready.set()
        self._loop.run_forever()

    async def _connect_forever(self) -> None:
        while True:
            try:
                async with websockets.connect(self._url, max_size=None) as ws:
                    self._ws = ws
                    logger.info("kernel proxy: connected to %s", self._url)
                    async for raw in ws:
                        self._dispatch_inbound(raw)
            except Exception as exc:
                logger.debug("kernel proxy: connection lost (%s); retrying", exc)
                self._fail_pending(ConnectionError("kernel proxy: connection lost"))
            finally:
                self._ws = None
            await asyncio.sleep(self._reconnect_delay)

    def _dispatch_inbound(self, raw: str | bytes) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("kernel proxy: bad JSON inbound: %r", raw[:200])
            return
        op = data.get("op")
        if op == "iopub":
            try:
                self._on_iopub(data)
            except Exception:
                logger.exception("kernel proxy: iopub handler raised")
            return
        # Anything else is a reply to a pending call.
        rid = data.get("id")
        if rid is None:
            return
        future = self._pending.pop(rid, None)
        if future is not None and not future.done():
            future.set_result(data)

    def _fail_pending(self, exc: BaseException) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    # --- caller-side ---

    def _send_payload_async(self, payload: dict[str, Any]) -> asyncio.Future[None]:
        """Schedule a send on the loop thread; returns a Future for the send."""
        if self._loop is None:
            raise RuntimeError("kernel proxy: loop not initialized")
        encoded = json.dumps(payload, default=_json_default)

        async def _do_send() -> None:
            if self._ws is None:
                raise ConnectionError("kernel proxy: not connected")
            await self._ws.send(encoded)

        return asyncio.run_coroutine_threadsafe(_do_send(), self._loop)  # type: ignore[return-value]

    def send(self, payload: dict[str, Any], *, timeout: float = 5.0) -> None:
        """Fire-and-forget send (raises on send failure / disconnect)."""
        bg = self._send_payload_async(payload)
        bg.result(timeout=timeout)

    def call_sync(
        self,
        op: str,
        *,
        timeout: float = 600.0,
        **fields: Any,
    ) -> dict[str, Any]:
        """Send a request and block until a reply with matching id arrives."""
        if self._loop is None:
            raise RuntimeError("kernel proxy: loop not initialized")
        rid = uuid.uuid4().hex
        payload = {"op": op, "id": rid, **fields}
        encoded = json.dumps(payload, default=_json_default)

        async def _do_request() -> dict[str, Any]:
            if self._ws is None:
                raise ConnectionError("kernel proxy: not connected")
            assert self._loop is not None
            future = self._loop.create_future()
            self._pending[rid] = future
            try:
                await self._ws.send(encoded)
                return await asyncio.wait_for(future, timeout=timeout)
            finally:
                self._pending.pop(rid, None)

        bg = asyncio.run_coroutine_threadsafe(_do_request(), self._loop)
        try:
            return bg.result(timeout=timeout + 5)
        except TimeoutError:
            with contextlib.suppress(Exception):
                bg.cancel()
            raise
