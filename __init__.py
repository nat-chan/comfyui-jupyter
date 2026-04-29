from __future__ import annotations

import ast
import asyncio
import base64
import re
import threading
import typing as t
import uuid
from io import BytesIO
from pathlib import Path

import aiohttp
import IPython.core.page as _page_mod
import torch
from comfy_api.latest import ComfyExtension, io
from comfy_api_nodes.util.conversions import (  # noqa
    bytesio_to_image_tensor,
    pil_to_bytesio,
    tensor_to_pil,
)
from IPython.core.interactiveshell import InteractiveShell
from IPython.utils.capture import capture_output
from PIL import Image
from server import PromptServer  # noqa
from typing_extensions import override

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


def _resolve_function(func_src: str, func_name: str, embedded_code: str) -> t.Callable[..., t.Any]:
    if func_src == "jupyter kernel":
        if not func_name:
            raise ValueError("func_name is required when func_src is 'jupyter kernel'")
        candidate = _user_ns.get(func_name)
        if not callable(candidate):
            raise ValueError(f"{func_name!r} is not callable in jupyter namespace")
        return candidate
    if func_src == "embedded code":
        tree = ast.parse(embedded_code)
        func_defs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
        if len(func_defs) != 1:
            raise ValueError(
                f"embedded code must define exactly one root-level function, got {len(func_defs)}"
            )
        ns: dict[str, t.Any] = {}
        exec(compile(tree, "<embedded_code>", "exec"), ns, ns)
        return ns[func_defs[0].name]
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
                                io.String.Input(
                                    "embedded_code",
                                    default="def f(*args, **kwargs):\n    return args, kwargs",
                                    multiline=True,
                                ),
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
    def execute(
        cls,
        func_src: dict[str, t.Any],
        **kwargs: t.Any,
    ) -> io.NodeOutput:
        selection: str = func_src["func_src"]
        func_name: str = func_src.get("func_name", "") or ""
        embedded_code: str = func_src.get("embedded_code", "") or ""
        func = _resolve_function(selection, func_name, embedded_code)
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


def _no_pager(
    strng: str | dict[str, str], start: int = 0, screen_lines: int = 0, pager_cmd: str | None = None
) -> None:  # noqa: E501
    """ページャーの代わりに stdout に直接出力する。"""
    if isinstance(strng, dict):
        strng = strng.get("text/plain", "")
    print(strng)


_page_mod.page = _no_pager  # type: ignore[assignment]
_page_mod.display_page = _no_pager  # type: ignore[assignment]

_shell: InteractiveShell = InteractiveShell.instance(user_ns=_user_ns)
_OUT_RE: re.Pattern[str] = re.compile(r"^Out\[\d+\]: .*\n?", re.MULTILINE)


def _sanitize_for_json(obj: t.Any) -> t.Any:
    """MIME bundle 内の bytes を base64 に変換し JSON シリアライズ可能にする。"""
    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode("ascii")
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    return obj


def _flush_matplotlib_figures() -> list[tuple[dict[str, str], dict[str, t.Any]]]:
    """開いている matplotlib の figure を PNG にレンダリングして閉じる。"""
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    figs: list[tuple[dict[str, str], dict[str, t.Any]]] = []
    for fig_num in plt.get_fignums():
        fig = plt.figure(fig_num)
        buf = BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        buf.seek(0)
        png_b64: str = base64.b64encode(buf.read()).decode("ascii")
        figs.append(
            (
                {"image/png": png_b64, "text/plain": repr(fig)},
                {},
            )
        )
    plt.close("all")
    return figs


def _run_cell(code: str) -> dict[str, t.Any]:
    """InteractiveShell でコードを実行し、リッチ出力を含む結果を返す。"""
    with capture_output(stdout=True, stderr=True, display=True) as captured:
        result = _shell.run_cell(code, silent=False, store_history=True)

    # display() 経由の出力を MIME bundle リストに変換
    display_data: list[t.Any] = [
        o._repr_mimebundle_()
        for o in captured.outputs  # type: ignore[union-attr]
    ]

    # matplotlib の figure を手動キャプチャ (%matplotlib inline 不要)
    display_data.extend(_flush_matplotlib_figures())

    # 最後の式の値を MIME bundle に変換
    execute_result: dict[str, t.Any] | None = None
    if result.result is not None:
        fmt_data, fmt_md = _shell.display_formatter.format(result.result)
        execute_result = {"data": fmt_data, "metadata": fmt_md}

    # stdout から Out[N]: ... 行を除去 (execute_result で別途送るため)
    stdout: str = _OUT_RE.sub("", captured.stdout)

    if result.success:
        return {
            "status": "ok",
            "stdout": stdout,
            "stderr": captured.stderr,
            "display_data": display_data,
            "execute_result": execute_result,
        }

    # エラー時: InteractiveShell はトレースバックを stdout に出力する
    err: BaseException = result.error_in_exec or result.error_before_exec  # type: ignore[assignment]
    return {
        "status": "error",
        "stdout": "",
        "stderr": captured.stderr,
        "display_data": display_data,
        "execute_result": None,
        "ename": type(err).__name__,
        "evalue": str(err),
        "traceback": stdout.splitlines(),
    }


@routes.post("/comfyui_jupyter/execute_code")
async def _execute_code(request: aiohttp.web.Request) -> aiohttp.web.Response:
    data = await request.json()
    code: str = data.get("code", "")
    # run_cell は同期関数なので別スレッドで実行し、イベントループをブロックしない。
    # これにより run_cell 内で tools.trigger_queue(wait=True) を呼んでも
    # イベントループが WS メッセージを処理できるためデッドロックしない。
    result = await asyncio.to_thread(_run_cell, code)
    return aiohttp.web.json_response(_sanitize_for_json(result))


@routes.post("/comfyui_jupyter/complete")
async def _complete(request: aiohttp.web.Request) -> aiohttp.web.Response:
    from IPython.core.completer import provisionalcompleter, rectify_completions

    data = await request.json()
    code: str = data.get("code", "")
    cursor_pos: int = data.get("cursor_pos", 0)
    with provisionalcompleter():
        raw = _shell.Completer.completions(code, cursor_pos)
        completions = list(rectify_completions(code, raw))
    if completions:
        matches: list[str] = [c.text for c in completions]
        start: int = completions[0].start
        end: int = completions[0].end
        types: list[dict[str, str | int]] = [
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
        matches = []
        start = cursor_pos
        end = cursor_pos
        types = []
    return aiohttp.web.json_response(
        {
            "matches": matches,
            "cursor_start": start,
            "cursor_end": end,
            "_jupyter_types_experimental": types,
        }
    )


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
