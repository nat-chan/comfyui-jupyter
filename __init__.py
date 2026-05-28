from __future__ import annotations

import asyncio
import base64
import datetime
import json
import logging
import os
import re
import sys
import threading
import typing as t
import uuid
from io import BytesIO
from pathlib import Path

import aiohttp
import comm
import comm.base_comm
import torch
from comfy_api.latest import ComfyExtension, io
from comfy_api_nodes.util.conversions import (  # noqa
    bytesio_to_image_tensor,
    pil_to_bytesio,
    tensor_to_pil,
)
from ipykernel.zmqshell import ZMQInteractiveShell
from jupyter_client.session import Session
from PIL import Image
from server import PromptServer  # noqa
from typing_extensions import override

logger = logging.getLogger(__name__)

WEB_DIRECTORY = "./web"
__all__ = ["WEB_DIRECTORY", "comfy_entrypoint"]


"""
https://zenn.dev/4kk11/articles/4e36fc68293bd2
https://github.com/chrisgoringe/Comfy-Custom-Node-How-To/wiki/api
"""


routes: aiohttp.web_routedef.RouteTableDef = PromptServer.instance.routes
app: aiohttp.web_app.Application = PromptServer.instance.app


# tools.wait_prompt が完了通知を受け取るための仕組み。
# PromptQueue.history を __setitem__ で通知する dict subclass に差し替える。
_completion_events: dict[str, threading.Event] = {}
_completion_lock = threading.Lock()


class _NotifyingHistory(dict[str, t.Any]):
    """history に prompt_id が追加されたタイミングで待機中の Event を set する dict。"""

    def __setitem__(self, key: str, value: t.Any) -> None:
        super().__setitem__(key, value)
        with _completion_lock:
            event = _completion_events.get(key)
        if event is not None:
            event.set()


def _install_history_hook() -> None:
    queue = PromptServer.instance.prompt_queue
    queue.history = _NotifyingHistory(queue.history)

    # wipe_history は `self.history = {}` で通常 dict に戻してしまうのでラップする。
    original_wipe = queue.wipe_history

    def wipe_history() -> None:
        original_wipe()
        queue.history = _NotifyingHistory(queue.history)

    queue.wipe_history = wipe_history


_install_history_hook()


# ユーザに公開する便利ツールの名前空間
class tools:
    tensor_to_pil = tensor_to_pil

    @staticmethod
    def pil_to_tensor(img: Image.Image, mode: str = "RGB") -> torch.Tensor:
        """PIL.Image -> ComfyUI image tensor (1, H, W, C), float32, 0-1."""
        return bytesio_to_image_tensor(pil_to_bytesio(img.convert(mode)), mode=mode)

    @staticmethod
    def file_to_tensor(path: str | Path, mode: str = "RGB") -> torch.Tensor:
        """File path -> ComfyUI image tensor (1, H, W, C)."""
        with open(path, "rb") as f:
            return bytesio_to_image_tensor(BytesIO(f.read()), mode=mode)

    @staticmethod
    def list_sids() -> list[str]:
        """現在 WebSocket 接続中のクライアント sid を列挙する。

        queue_prompt(sid=...) で対象ブラウザを指定する際の候補取得に使う。
        """
        return list(PromptServer.instance.sockets.keys())

    @staticmethod
    def queue_prompt(sid: str | None = None) -> str:
        """ブラウザで開いているワークフローの実行をトリガーし、prompt_id を返す。

        JS 側で api.queuePrompt をインターセプトし、得られた prompt_id を
        POST /comfyui_jupyter/queue_prompt_result でコールバックする。

        Args:
            sid: 対象クライアントID。省略時は全クライアントにブロードキャスト。

        Returns:
            prompt_id (str)
        """
        loop = PromptServer.instance.loop
        future = asyncio.run_coroutine_threadsafe(_queue_prompt_async(sid=sid), loop)
        return future.result(timeout=30)

    @staticmethod
    def wait_prompt(prompt_id: str, timeout: float = 600) -> dict[str, t.Any]:
        """prompt_id の実行が完了するまで待機する。

        PromptQueue.history の __setitem__ フックで threading.Event を set する仕組みを使う。
        成功/失敗/中断いずれのケースでも history に結果が書き込まれるため、全状況で動作する。

        Args:
            prompt_id: 待機対象の prompt_id。
            timeout:   最大待機秒数 (デフォルト 600秒)。

        Returns:
            history に格納された結果 dict。タイムアウト時は {"status": "timeout"}。
        """
        queue = PromptServer.instance.prompt_queue

        event = threading.Event()
        with _completion_lock:
            _completion_events[prompt_id] = event

        try:
            # Event 登録前に既に完了済みなら即座に返す。登録後の完了は __setitem__ が拾う。
            with queue.mutex:
                if prompt_id in queue.history:
                    return queue.history[prompt_id]

            if not event.wait(timeout):
                return {"status": "timeout"}

            with queue.mutex:
                return queue.history.get(prompt_id, {"status": "unknown"})
        finally:
            with _completion_lock:
                _completion_events.pop(prompt_id, None)


# コード実行間で変数を保持する名前空間
_user_ns: dict[str, t.Any] = {"tools": tools}


# {{{ node ---
class JupyterSave(io.ComfyNode):
    @classmethod
    @override
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="JupyterSave",
            display_name="Jupyter Save",
            category="comfyui-jupyter",
            inputs=[
                io.String.Input("key", default="a", multiline=False),
                io.AnyType.Input("value"),
            ],
            outputs=[],
            is_output_node=True,
        )

    @classmethod
    @override
    def execute(cls, key: str, value: t.Any) -> io.NodeOutput:
        _user_ns[key] = value
        return io.NodeOutput()


class JupyterLoad(io.ComfyNode):
    @classmethod
    @override
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="JupyterLoad",
            display_name="Jupyter Load",
            category="comfyui-jupyter",
            inputs=[
                io.String.Input("key", default="a", multiline=False),
            ],
            outputs=[
                io.AnyType.Output("value"),
            ],
            is_output_node=True,
        )

    @classmethod
    @override
    def fingerprint_inputs(cls, **kwargs: t.Any) -> float:
        return float("nan")

    @classmethod
    @override
    def execute(cls, key: str) -> io.NodeOutput:
        return io.NodeOutput(_user_ns.get(key, None))


_ARG_KEY_RE = re.compile(r"^arg_(\d+)$")
_ARG_NAME_KEY_RE = re.compile(r"^argname_(\d+)$")


def _resolve_function(
    func_src: str,
    func_name: str,
    embedded_code: str,
    file_path: str,
) -> t.Callable[..., t.Any]:
    if not func_name:
        raise ValueError("func_name is required")
    if func_src == "jupyter kernel":
        candidate = _user_ns.get(func_name)
        if not callable(candidate):
            raise ValueError(f"{func_name!r} is not callable in jupyter namespace")
        return candidate
    if func_src == "embedded code":
        # Evaluate the whole embedded source so imports and helper definitions
        # at module scope take effect, then pick out the function named
        # `func_name`. This lets users paste long Jupyter scripts verbatim.
        ns: dict[str, t.Any] = {}
        exec(compile(embedded_code, "<embedded_code>", "exec"), ns, ns)
        candidate = ns.get(func_name)
        if not callable(candidate):
            raise ValueError(f"{func_name!r} is not defined as a callable in embedded code")
        return candidate
    if func_src == "from file":
        if not file_path:
            raise ValueError("file_path is required when func_src is 'from file'")
        with open(file_path, encoding="utf-8") as fp:
            source = fp.read()
        ns = {}
        exec(compile(source, file_path, "exec"), ns, ns)
        candidate = ns.get(func_name)
        if not callable(candidate):
            raise ValueError(f"{func_name!r} is not defined as a callable in {file_path}")
        return candidate
    raise ValueError(f"unknown func_src: {func_src!r}")


def _build_call_args(
    kwargs: dict[str, t.Any],
) -> tuple[list[t.Any], dict[str, t.Any]]:
    """Pair `arg_i` sockets with `argname_i` widgets in slot index order.

    Empty `argname_i` -> positional arg, non-empty -> keyword arg with that name.

    With Nodes 2.0 inline rendering each `arg_i` input is paired with its
    `argname_i` widget (`slot.widget = {name: argname_i}`). When the socket is
    unconnected, ComfyUI falls back to sending the widget's value as the input
    value — i.e. `kwargs[arg_i] == kwargs[argname_i]` (both strings). We detect
    that pattern and skip those slots so unconnected pairs don't leak into the
    Python call.
    """
    indices: dict[int, t.Any] = {}
    names: dict[int, str] = {}
    for key, value in kwargs.items():
        m = _ARG_KEY_RE.match(key)
        if m is not None:
            indices[int(m.group(1))] = value
            continue
        n = _ARG_NAME_KEY_RE.match(key)
        if n is not None and isinstance(value, str):
            names[int(n.group(1))] = value
    positional: list[t.Any] = []
    keyword: dict[str, t.Any] = {}
    for i in sorted(indices):
        value = indices[i]
        name = (names.get(i) or "").strip()
        argname_raw = names.get(i, "")
        if isinstance(value, str) and value == argname_raw:
            continue
        if name:
            keyword[name] = value
        else:
            positional.append(value)
    return positional, keyword


class JupyterFunction(io.ComfyNode):
    @classmethod
    @override
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="JupyterFunction",
            display_name="Jupyter Function",
            category="comfyui-jupyter",
            inputs=[
                io.DynamicCombo.Input(
                    "func_src",
                    options=[
                        io.DynamicCombo.Option(
                            "jupyter kernel",
                            [io.String.Input("func_name", default="f")],
                        ),
                        io.DynamicCombo.Option(
                            "embedded code",
                            [
                                # `func_name` is in every branch so it stays
                                # visible regardless of `func_src`; placing it
                                # before the source widget pins the UI order to
                                # func_src → func_name → (source widget).
                                io.String.Input("func_name", default="f"),
                                io.String.Input(
                                    "embedded_code",
                                    default="def f(*args, **kwargs):\n    return args, kwargs",
                                    multiline=True,
                                ),
                            ],
                        ),
                        io.DynamicCombo.Option(
                            "from file",
                            [
                                io.String.Input("func_name", default="f"),
                                io.String.Input("file_path", default="/path/to/file.py"),
                            ],
                        ),
                    ],
                ),
            ],
            outputs=[io.AnyType.Output("retval")],
            accept_all_inputs=True,
        )

    @classmethod
    @override
    def fingerprint_inputs(cls, **kwargs: t.Any) -> float:
        # The visible inputs do not capture every source of change:
        # `jupyter kernel` may rebind `func_name` to a new body, and
        # `from file` reads disk content that the cache cannot see. Returning
        # NaN (which never equals itself) forces ComfyUI to re-execute.
        return float("nan")

    @classmethod
    @override
    def execute(
        cls,
        func_src: dict[str, t.Any],
        **kwargs: t.Any,
    ) -> io.NodeOutput:
        selection: str = func_src["func_src"]
        func_name: str = func_src.get("func_name", "") or ""
        embedded_code: str = func_src.get("embedded_code", "") or ""
        file_path: str = func_src.get("file_path", "") or ""
        func = _resolve_function(selection, func_name, embedded_code, file_path)
        positional, keyword = _build_call_args(kwargs)
        return io.NodeOutput(func(*positional, **keyword))


class JupyterExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [JupyterSave, JupyterLoad, JupyterFunction]


async def comfy_entrypoint() -> JupyterExtension:
    return JupyterExtension()


# --- node }}}

# {{{ server ---

# Cross-process kernel transparency.
#
# The comfyui_kernel process (separate Python venv, what JupyterLab actually
# connects to) forwards every shell request to ComfyUI over a single
# `/comfyui_jupyter/proxy` WebSocket. Code runs against the InteractiveShell
# below — same process as PromptServer, so tensors and `tools.*` retain
# zero-copy access. ipywidgets / plotly / matplotlib publish iopub messages
# via the FakeKernel infrastructure; those messages travel back over the
# same WS and the comfyui_kernel re-emits them on its real iopub socket.


# --- FakeKernel: ipywidgets-shaped attributes mounted on a non-kernel shell ---


_kernel_ws: aiohttp.web.WebSocketResponse | None = None
_kernel_ws_loop: asyncio.AbstractEventLoop | None = None
_kernel_ws_lock = threading.Lock()


def _json_default(obj: t.Any) -> str:
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode("ascii")
    return str(obj)


def _kernel_ws_send(payload: dict[str, t.Any]) -> None:
    """Forward `payload` to the attached kernel from any thread (best-effort).

    Drops silently when no kernel is attached — widgets created without a
    listening kernel just never become visible, which is harmless.
    """
    with _kernel_ws_lock:
        ws = _kernel_ws
        loop = _kernel_ws_loop
    if ws is None or loop is None:
        return
    encoded = json.dumps(payload, default=_json_default)
    asyncio.run_coroutine_threadsafe(ws.send_str(encoded), loop)


class _WSSocket:
    """Duck-typed ZMQ socket for `jupyter_client.Session.send`.

    `Session.send` serializes the message into wire parts and then calls
    `socket.send_multipart(parts)`. We deserialize the parts back into a
    logical message dict (using the same session) and forward over WS as
    JSON so the kernel side can re-sign and re-emit with its own session.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def send_multipart(
        self,
        parts: list[bytes],
        copy: bool = False,  # noqa: ARG002
        track: bool = False,  # noqa: ARG002
    ) -> None:
        try:
            _idents, msg_parts = self._session.feed_identities(parts, copy=True)
            msg = self._session.deserialize(msg_parts, content=True, copy=True)
        except Exception:
            logger.exception("comfyui_jupyter: WSSocket failed to decode parts")
            return
        buffers = msg.get("buffers") or []
        _kernel_ws_send(
            {
                "op": "iopub",
                "msg_type": msg["header"]["msg_type"],
                "header": msg["header"],
                "parent_header": msg.get("parent_header") or {},
                "metadata": msg.get("metadata") or {},
                "content": msg.get("content") or {},
                "buffers": [base64.b64encode(bytes(b)).decode("ascii") for b in buffers],
            }
        )


class _FakeKernel:
    """The 4 attributes ipywidgets and the IPython publisher chain require."""

    def __init__(self) -> None:
        # Empty signing key — Session won't sign; receiver doesn't verify.
        self.session = Session()
        self.iopub_socket = _WSSocket(self.session)
        self._parent: dict[str, t.Any] = {}
        self.comm_manager = comm.base_comm.CommManager()

    def get_parent(self, channel: str | None = None) -> dict[str, t.Any]:  # noqa: ARG002
        return self._parent

    def set_parent(self, parent_msg: dict[str, t.Any] | None) -> None:
        self._parent = parent_msg or {}


class _ForwardingComm(comm.base_comm.BaseComm):
    """Comm whose `publish_msg` writes via FakeKernel.session/iopub_socket."""

    def publish_msg(  # type: ignore[override]
        self,
        msg_type: str,
        data: dict[str, t.Any] | None = None,
        metadata: dict[str, t.Any] | None = None,
        buffers: list[bytes] | None = None,
        **keys: t.Any,
    ) -> None:
        data = data if data is not None else {}
        metadata = metadata if metadata is not None else {}
        content = dict(keys)
        content["comm_id"] = self.comm_id
        content["data"] = data
        if msg_type == "comm_open":
            content["target_name"] = self.target_name
            if self.target_module:
                content["target_module"] = self.target_module
        _fake_kernel.session.send(
            _fake_kernel.iopub_socket,
            msg_type,
            content,
            metadata=metadata,
            parent=_fake_kernel.get_parent(),
            ident=self.topic,
            buffers=buffers,
        )


_fake_kernel = _FakeKernel()


def _our_create_comm(
    target_name: str = "",
    data: dict[str, t.Any] | None = None,
    metadata: dict[str, t.Any] | None = None,
    buffers: list[bytes] | None = None,
    **kwargs: t.Any,
) -> _ForwardingComm:
    return _ForwardingComm(
        target_name=target_name, data=data, metadata=metadata, buffers=buffers, **kwargs
    )


def _our_get_comm_manager() -> comm.base_comm.CommManager:
    return _fake_kernel.comm_manager


# Replace `comm` package singletons BEFORE any code imports `ipywidgets`.
# `ipykernel.ipkernel` would otherwise rebind these to its own factory,
# which targets a Kernel.instance() that does not exist in this process.
comm.create_comm = _our_create_comm
comm.get_comm_manager = _our_get_comm_manager


# --- shell setup ---


_shell: ZMQInteractiveShell = ZMQInteractiveShell.instance(user_ns=_user_ns)
# `Output` widget (and a few other things) read shell.kernel for parent-
# header threading. Point it at our fake.
_shell.kernel = _fake_kernel
# Wire publishing chain (displayhook = `_` value, display_pub = display(),
# pyout = legacy) so iopub messages emitted by the shell flow through our
# FakeKernel session/socket pair.
_shell.displayhook.session = _fake_kernel.session
_shell.displayhook.pub_socket = _fake_kernel.iopub_socket
_shell.displayhook.topic = b"execute_result"
_shell.display_pub.session = _fake_kernel.session
_shell.display_pub.pub_socket = _fake_kernel.iopub_socket


# --- shell default overrides ---
#
# Third-party libraries occasionally publish via MIME types that need a
# JupyterLab extension we don't bundle (e.g. plotly's
# `application/vnd.plotly.v1+json` needs `jupyterlab-plotly` or `anywidget`,
# matplotlib defaults to an Agg backend outside ipykernel, ...). Rather
# than ship every extension, we pin each library's default to a behaviour
# that renders against vanilla JupyterLab.
#
# Strategy: each override is a function that runs once at module load.
# Most libraries we care about expose an environment-variable hook they
# read at their own first import — `PLOTLY_RENDERER`, `MPLBACKEND`, etc.
# Using `os.environ.setdefault` means: if the user has explicitly chosen
# the library's native default (because they installed the matching
# JupyterLab extension and set the env var themselves), we don't clobber
# it. For libraries already imported when our extension loads, the apply
# function falls back to mutating the live config directly.
#
# Users can also opt out per-name (or fully) without touching env vars
# the library cares about:
#
#     export COMFYUI_JUPYTER_DISABLE_DEFAULTS=plotly,matplotlib
#     export COMFYUI_JUPYTER_DISABLE_DEFAULTS=all
#
# To add a new override: define an apply function below and append it to
# `_DEFAULTS`.

_disabled_defaults: set[str] = {
    s.strip().lower()
    for s in os.environ.get("COMFYUI_JUPYTER_DISABLE_DEFAULTS", "").split(",")
    if s.strip()
}


def _apply_plotly_default() -> None:
    """Pin plotly's default renderer to a JupyterLab-extension-free one.

    plotly's auto-detection in a `ZMQInteractiveShell` sets the renderer
    to `plotly_mimetype`, which produces only `application/vnd.plotly.v1+json`
    and requires `jupyterlab-plotly` / `anywidget` on the frontend.
    `notebook_connected` emits `text/html` with a CDN-loaded plotly.js
    bundle, which JupyterLab renders natively.

    plotly reads `PLOTLY_RENDERER` once when `plotly.io` first imports.
    For not-yet-imported plotly: `setdefault` wins. For already-imported
    plotly: assign through to the live config.
    """
    os.environ.setdefault("PLOTLY_RENDERER", "notebook_connected")
    plotly_io = sys.modules.get("plotly.io")
    if plotly_io is not None:
        plotly_io.renderers.default = "notebook_connected"


def _apply_matplotlib_default() -> None:
    """Switch matplotlib to the inline backend so `plt.show()` produces output.

    Outside ipykernel, matplotlib defaults to `agg` (a non-interactive
    raster backend that swallows `plt.show()` silently). The inline
    backend bundled with ipykernel emits `display_data` containing PNG
    image data when `plt.show()` is called, which goes through our
    FakeKernel iopub forwarder to JupyterLab.

    matplotlib reads `MPLBACKEND` on its first import — `setdefault`
    means the user's own choice is preserved if set. For already-imported
    matplotlib (without pyplot yet), `matplotlib.use(...)` is a clean
    swap; if pyplot is also loaded, matplotlib warns but the switch still
    takes effect.
    """
    os.environ.setdefault("MPLBACKEND", "module://matplotlib_inline.backend_inline")
    mpl = sys.modules.get("matplotlib")
    if mpl is not None:
        try:
            mpl.use("module://matplotlib_inline.backend_inline")
        except Exception:
            logger.exception("comfyui_jupyter: matplotlib.use() failed")


# (override id, apply function)
_DEFAULTS: list[tuple[str, t.Callable[[], None]]] = [
    ("plotly", _apply_plotly_default),
    ("matplotlib", _apply_matplotlib_default),
]


def _apply_all_defaults() -> None:
    if "all" in _disabled_defaults:
        return
    for name, apply_fn in _DEFAULTS:
        if name in _disabled_defaults:
            continue
        try:
            apply_fn()
        except Exception:
            logger.exception("comfyui_jupyter: %s default override failed", name)


_apply_all_defaults()


def _run_cell_for_kernel(
    code: str,
    parent_header: dict[str, t.Any],
    silent: bool,
    store_history: bool,
) -> dict[str, t.Any]:
    """Run a cell under a parent_header so iopub correlates to the right cell."""
    parent_msg = {"header": parent_header} if parent_header else {}
    _fake_kernel.set_parent(parent_msg)
    _shell.displayhook.set_parent(parent_msg)
    _shell.display_pub.set_parent(parent_msg)
    try:
        result = _shell.run_cell(code, silent=silent, store_history=store_history)
    finally:
        _fake_kernel.set_parent({})
    exec_count = _shell.execution_count - 1 if store_history else 0
    if result.success:
        return {
            "status": "ok",
            "execution_count": exec_count,
            "payload": [],
            "user_expressions": {},
        }
    err = result.error_in_exec or result.error_before_exec
    return {
        "status": "error",
        "execution_count": exec_count,
        "ename": type(err).__name__ if err is not None else "Error",
        "evalue": str(err) if err is not None else "",
        "traceback": [],
    }


def _complete_for_kernel(code: str, cursor_pos: int) -> dict[str, t.Any]:
    from IPython.core.completer import provisionalcompleter, rectify_completions

    with provisionalcompleter():
        raw = _shell.Completer.completions(code, cursor_pos)
        completions = list(rectify_completions(code, raw))
    if completions:
        matches = [c.text for c in completions]
        start = completions[0].start
        end = completions[0].end
        types = [
            {
                "text": c.text,
                "type": c.type or "",
                "signature": c.signature or "",
                "start": c.start,
                "end": c.end,
            }
            for c in completions
        ]
    else:
        matches, start, end, types = [], cursor_pos, cursor_pos, []
    return {
        "status": "ok",
        "matches": matches,
        "cursor_start": start,
        "cursor_end": end,
        "metadata": {"_jupyter_types_experimental": types},
    }


# --- WS endpoint ---


@routes.get("/comfyui_jupyter/proxy")
async def _kernel_proxy_ws(request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
    """The comfyui_kernel process connects here on startup.

    A single attachment at a time — if a fresh kernel connects while one is
    already attached, we replace the reference. Old connection will close on
    next send when the new one takes over.
    """
    global _kernel_ws, _kernel_ws_loop
    ws = aiohttp.web.WebSocketResponse(heartbeat=30, max_msg_size=0)
    await ws.prepare(request)
    with _kernel_ws_lock:
        _kernel_ws = ws
        _kernel_ws_loop = asyncio.get_event_loop()
    logger.info("comfyui_jupyter proxy: kernel attached from %s", request.remote)
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    logger.warning("comfyui_jupyter proxy: bad JSON inbound")
                    continue
                asyncio.create_task(_handle_proxy_msg(ws, payload))
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.warning("comfyui_jupyter proxy: ws error %s", ws.exception())
    finally:
        with _kernel_ws_lock:
            if _kernel_ws is ws:
                _kernel_ws = None
                _kernel_ws_loop = None
        logger.info("comfyui_jupyter proxy: kernel detached")
    return ws


async def _handle_proxy_msg(
    ws: aiohttp.web.WebSocketResponse,
    payload: dict[str, t.Any],
) -> None:
    op = payload.get("op")
    if op == "execute":
        rid = payload.get("id")
        code: str = payload.get("code", "")
        silent: bool = bool(payload.get("silent", False))
        store_history: bool = bool(payload.get("store_history", True))
        parent_header: dict[str, t.Any] = payload.get("parent_header") or {}
        content = await asyncio.to_thread(
            _run_cell_for_kernel,
            code,
            parent_header,
            silent,
            store_history,
        )
        await ws.send_str(
            json.dumps({"op": "execute_reply", "id": rid, "content": content}, default=_json_default)
        )
    elif op == "complete":
        rid = payload.get("id")
        code = payload.get("code", "")
        cursor_pos = int(payload.get("cursor_pos", 0))
        content = await asyncio.to_thread(_complete_for_kernel, code, cursor_pos)
        await ws.send_str(
            json.dumps({"op": "complete_reply", "id": rid, "content": content}, default=_json_default)
        )
    elif op in ("comm_open", "comm_msg", "comm_close"):
        msg = payload.get("msg") or {}
        buffers_b64 = msg.get("buffers") or []
        msg["buffers"] = [base64.b64decode(b) for b in buffers_b64]
        handler = getattr(_fake_kernel.comm_manager, op)
        try:
            handler(None, b"", msg)
        except Exception:
            logger.exception("comfyui_jupyter proxy: %s dispatch failed", op)
    else:
        logger.warning("comfyui_jupyter proxy: unknown op %r", op)


# queue_prompt: JS からのコールバックで prompt_id (またはエラー) を受け取る
_pending_queue_prompts: dict[str, asyncio.Future[dict[str, t.Any]]] = {}


class QueuePromptError(RuntimeError):
    """queue_prompt で validation エラー等が発生した場合に送出される。"""

    def __init__(self, error: dict[str, t.Any]) -> None:
        self.error = error
        # error は {"error": {"type": "prompt_no_outputs", "message": "...", ...}, "node_errors": {...}}
        # のような構造
        message: str = (
            error.get("error", {}).get("message", str(error))
            if isinstance(error.get("error"), dict)
            else str(error.get("error", error))
        )
        super().__init__(message)


async def _queue_prompt_async(sid: str | None = None) -> str:
    """ブラウザの queuePrompt をトリガーし、prompt_id を受け取って返す。

    流れ:
        1. request_id を生成し WS でブラウザに送信
        2. JS が app.queuePrompt(0) → api.queuePrompt のインターセプトで prompt_id を取得
        3. JS が POST /comfyui_jupyter/queue_prompt_result で結果を返す
        4. Future が解決されこの関数が返る

    Raises:
        QueuePromptError: validation エラー等で prompt の投入に失敗した場合
    """
    request_id = uuid.uuid4().hex
    loop = asyncio.get_event_loop()
    _pending_queue_prompts[request_id] = loop.create_future()

    PromptServer.instance.send_sync(
        "comfyui_jupyter/queue_prompt",
        {"request_id": request_id},
        sid,
    )

    try:
        result: dict[str, t.Any] = await asyncio.wait_for(
            _pending_queue_prompts[request_id],
            timeout=30,
        )
    finally:
        _pending_queue_prompts.pop(request_id, None)

    if "error" in result:
        raise QueuePromptError(result["error"])

    prompt_id: str | None = result.get("prompt_id")
    if prompt_id is None:
        raise QueuePromptError({"error": "prompt_id not received from browser"})

    return prompt_id


@routes.post("/comfyui_jupyter/queue_prompt_result")
async def _queue_prompt_result(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """JS 側からのコールバック。queuePrompt の結果 (prompt_id またはエラー) を受け取る。"""
    data: dict[str, t.Any] = await request.json()
    request_id: str | None = data.get("request_id")
    if request_id is not None:
        future = _pending_queue_prompts.get(request_id)
        if future is not None and not future.done():
            future.set_result(data)
    return aiohttp.web.json_response({"status": "ok"})


# --- server }}}
