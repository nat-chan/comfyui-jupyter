"""ComfyUI Jupyter Kernel — transparent proxy to ComfyUI's InteractiveShell.

The user's shell, namespace, models, and tensors live in the ComfyUI process.
This kernel is the standard ipykernel exposed to JupyterLab; every shell
request gets forwarded over `KernelProxy` to ComfyUI, and every iopub
message ComfyUI emits is re-published on this kernel's real iopub socket.

What changes from a stock IPythonKernel:
  - `do_execute` / `do_complete` forward over WS instead of running locally.
  - `shell_handlers["comm_open" / "comm_msg" / "comm_close"]` are replaced
    with forwarders so widget messages from JupyterLab reach the *real*
    Comm objects living in ComfyUI's comm_manager.
  - A background iopub re-publisher takes messages off the WS and emits
    them on our `self.iopub_socket`, preserving the original parent_header
    so the frontend correlates streaming output with the right cell.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

from ipykernel.ipkernel import IPythonKernel

from comfyui_kernel.proxy import KernelProxy

logger = logging.getLogger(__name__)


def _bridge_url() -> str:
    base = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://") :]
    else:
        ws_base = base
    return ws_base.rstrip("/") + "/comfyui_jupyter/proxy"


class ComfyUIKernel(IPythonKernel):
    implementation = "comfyui"
    implementation_version = "0.3.0"
    banner = (
        "ComfyUI Kernel — transparent proxy. Shell, tensors, models, and "
        "`tools` live in the ComfyUI process."
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._proxy = KernelProxy(url=_bridge_url(), on_iopub=self._republish_iopub)
        self._install_comm_forwarders()
        logger.info("comfyui kernel ready (proxy=%s)", _bridge_url())

    # --- comm forwarding (frontend -> ComfyUI) ---

    def _install_comm_forwarders(self) -> None:
        """Replace stock comm_* handlers with forwarders to ComfyUI."""
        for msg_type in ("comm_open", "comm_msg", "comm_close"):
            self.shell_handlers[msg_type] = self._make_comm_forwarder(msg_type)

    def _make_comm_forwarder(self, msg_type: str):
        async def handler(stream, ident, msg):  # noqa: ANN001, ARG001
            # The full msg is what comm_manager.<comm_*> consumes on the
            # other side. `stream`/`ident` are ZMQ-specific and ignored
            # downstream (see comm/base_comm.py `comm_msg`/`comm_open`/
            # `comm_close`), so we drop them.
            try:
                self._proxy.send({"op": msg_type, "msg": _strip_msg(msg)})
            except Exception:
                logger.exception("kernel proxy: forwarding %s failed", msg_type)

        return handler

    # --- iopub republishing (ComfyUI -> frontend) ---

    def _republish_iopub(self, payload: dict[str, Any]) -> None:
        """Re-emit a forwarded iopub message on this kernel's real iopub.

        `payload` shape is the JSON we receive from ComfyUI:
            {"op": "iopub", "msg_type": ..., "header": ..., "parent_header": ...,
             "metadata": ..., "content": ..., "buffers": [<b64>, ...]}
        We don't reuse the inbound `header` (its session id won't match this
        kernel's) — we let `self.session.send` build a fresh header and pass
        the original `parent_header` for correlation.
        """
        msg_type = payload.get("msg_type")
        if not msg_type:
            return
        content = payload.get("content") or {}
        metadata = payload.get("metadata") or {}
        parent_header = payload.get("parent_header") or {}
        buffers = [base64.b64decode(b) for b in payload.get("buffers", [])]
        # session.send expects `parent` to be either a full msg dict or just
        # a header dict; it copies `parent["header"]` if present, else uses
        # `parent` directly. Wrap to be explicit.
        parent = {"header": parent_header} if parent_header else {}
        try:
            self.session.send(
                self.iopub_socket,
                msg_type,
                content=content,
                parent=parent,
                metadata=metadata,
                buffers=buffers if buffers else None,
            )
        except Exception:
            logger.exception("kernel proxy: republishing %s failed", msg_type)

    # --- request forwarding (frontend -> ComfyUI) ---

    async def do_execute(  # type: ignore[override]
        self,
        code: str,
        silent: bool,
        store_history: bool = True,
        user_expressions: dict[str, Any] | None = None,
        allow_stdin: bool = False,
        *,
        cell_id: str | None = None,
    ) -> dict[str, Any]:
        if not code.strip():
            return {
                "status": "ok",
                "execution_count": self.execution_count,
                "payload": [],
                "user_expressions": {},
            }
        parent = self.get_parent("shell")
        try:
            reply = self._proxy.call_sync(
                "execute",
                code=code,
                silent=silent,
                store_history=store_history,
                parent_header=parent.get("header") if parent else {},
            )
        except Exception as exc:
            logger.exception("kernel proxy: execute failed")
            return {
                "status": "error",
                "execution_count": self.execution_count,
                "ename": type(exc).__name__,
                "evalue": str(exc),
                "traceback": [f"{type(exc).__name__}: {exc}"],
            }
        return reply.get("content") or {
            "status": "error",
            "execution_count": self.execution_count,
            "ename": "BridgeError",
            "evalue": "no reply content",
            "traceback": [],
        }

    async def do_complete(  # type: ignore[override]
        self,
        code: str,
        cursor_pos: int,
    ) -> dict[str, Any]:
        try:
            reply = self._proxy.call_sync(
                "complete",
                code=code,
                cursor_pos=cursor_pos,
                timeout=10.0,
            )
        except Exception:
            logger.exception("kernel proxy: complete failed")
            return {
                "status": "ok",
                "matches": [],
                "cursor_start": cursor_pos,
                "cursor_end": cursor_pos,
                "metadata": {},
            }
        return reply.get("content") or {
            "status": "ok",
            "matches": [],
            "cursor_start": cursor_pos,
            "cursor_end": cursor_pos,
            "metadata": {},
        }


def _strip_msg(msg: dict[str, Any]) -> dict[str, Any]:
    """Trim a Jupyter wire-format message dict to what we can JSON-serialize.

    `msg["buffers"]` may contain bytes/memoryview — base64 them.
    Drop ZMQ-only fields like `tracker` that callers don't need.
    """
    buffers = msg.get("buffers") or []
    return {
        "header": msg.get("header"),
        "parent_header": msg.get("parent_header"),
        "metadata": msg.get("metadata"),
        "content": msg.get("content"),
        "buffers": [base64.b64encode(bytes(b)).decode("ascii") for b in buffers],
    }
