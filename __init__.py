from __future__ import annotations

import asyncio
import base64
import io
import re
import typing as t
from abc import ABCMeta
from io import BytesIO
from pathlib import Path

import aiohttp
import IPython.core.page as _page_mod
import torch
from comfy_api_nodes.util.conversions import (  # noqa
    bytesio_to_image_tensor,
    pil_to_bytesio,
    tensor_to_pil,
)
from IPython.core.interactiveshell import InteractiveShell
from IPython.utils.capture import capture_output
from PIL import Image
from server import PromptServer  # noqa

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = "./web"
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]


"""
https://zenn.dev/4kk11/articles/4e36fc68293bd2
https://github.com/chrisgoringe/Comfy-Custom-Node-How-To/wiki/api
"""


routes: aiohttp.web_routedef.RouteTableDef = PromptServer.instance.routes
app: aiohttp.web_app.Application = PromptServer.instance.app


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
    def trigger_queue(sid: str | None = None, wait: bool = True) -> dict[str, t.Any]:
        """ブラウザで開いているワークフローの実行をトリガーする。

        Jupyter セルは asyncio.to_thread 経由で別スレッドで実行されるため、
        run_coroutine_threadsafe でイベントループに処理を投げて完了を待つ。

        Args:
            sid:  対象クライアントID。省略時は全クライアントにブロードキャスト。
            wait: True なら推論完了まで待機。False なら即座に返る。
        """
        loop = PromptServer.instance.loop
        future = asyncio.run_coroutine_threadsafe(_trigger_queue_async(sid=sid, wait=wait), loop)
        return future.result(timeout=660)


# コード実行間で変数を保持する名前空間
_user_ns: dict[str, t.Any] = {"tools": tools}


# {{{ node ---
def format_class_name(class_name: str) -> str:
    """先頭以外の大文字の前に空白を挟む"""
    formatted_name = re.sub(r"(?<!^)(?=[A-Z])", " ", class_name)
    return formatted_name


class CustomNodeMeta(ABCMeta):
    def __new__(
        cls,
        name: str,
        bases: tuple,
        attrs: dict,
    ) -> "CustomNodeMeta":
        global NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

        @classmethod
        def _(cls):
            return {"required": cls.REQUIRED}

        new_class = super().__new__(
            cls,
            name,
            bases,
            attrs
            | {
                "FUNCTION": "main",
                "CATEGORY": "Paint",
                "INPUT_TYPES": _,
            },
        )
        NODE_CLASS_MAPPINGS[name] = new_class
        NODE_DISPLAY_NAME_MAPPINGS[name] = format_class_name(name) + "🐍"
        return new_class


class AnyType(str):
    def __ne__(self, __value: object) -> bool:
        return False


any = AnyType("*")


class JupyterSave(metaclass=CustomNodeMeta):
    OUTPUT_NODE = True
    RETURN_TYPES = ()
    REQUIRED = {
        "key": ("STRING", {"multiline": False, "default": "a"}),
        "value": ("*", {}),
    }

    def main(
        self,
        key: str,
        value: t.Any,
    ) -> tuple:
        global _user_ns
        _user_ns[key] = value
        return ()


class JupyterLoad(metaclass=CustomNodeMeta):
    OUTPUT_NODE = True
    RETURN_TYPES = (any,)
    RETURN_NAMES = ("value",)
    REQUIRED = {
        "key": ("STRING", {"multiline": False, "default": "a"}),
    }

    @classmethod
    def IS_CHANGED(cls, *args, **kwargs):
        return float("nan")

    def main(self, key) -> tuple[t.Any]:
        global _user_ns
        return (_user_ns.get(key, None),)


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
        buf = io.BytesIO()
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


async def _trigger_queue_async(sid: str | None = None, wait: bool = True) -> dict[str, t.Any]:
    """trigger_queue の非同期実装。ルートハンドラと tools.trigger_queue の両方から使う。"""
    if not wait:
        PromptServer.instance.send_sync("comfyui_jupyter/trigger_queue", {}, sid)
        return {"status": "ok"}

    # 推論完了まで待機する:
    # 1. フロントエンドが queuePrompt → POST /prompt → prompt_id が発行される
    # 2. 実行完了時に executing イベント (node=None) が送られる
    # その executing イベントを横取りして完了を検知する
    done: asyncio.Future[str] = asyncio.get_event_loop().create_future()

    original_send = PromptServer.instance.send

    async def _intercept_send(event: str, data: dict[str, t.Any], sid: str | None = None) -> None:
        await original_send(event, data, sid)
        if event == "executing" and data.get("node") is None and not done.done():
            done.set_result(data.get("prompt_id", ""))

    PromptServer.instance.send = _intercept_send  # type: ignore[assignment]
    try:
        PromptServer.instance.send_sync("comfyui_jupyter/trigger_queue", {}, sid)
        prompt_id: str = await asyncio.wait_for(done, timeout=600)
    except asyncio.TimeoutError:
        return {"status": "timeout"}
    finally:
        PromptServer.instance.send = original_send  # type: ignore[assignment]

    return {"status": "ok", "prompt_id": prompt_id}


@routes.post("/comfyui_jupyter/trigger_queue")
async def _trigger_queue(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """ブラウザで開いているワークフローの実行をトリガーする。

    Parameters (JSON body, すべてオプション):
        sid:  対象クライアントID。省略時は全クライアントにブロードキャスト。
        wait: true にすると推論完了まで応答を返さない (デフォルト: true)。
    """
    data: dict[str, t.Any] = await request.json() if request.can_read_body else {}
    sid: str | None = data.get("sid", None)
    wait: bool = data.get("wait", True)
    result = await _trigger_queue_async(sid=sid, wait=wait)
    status_code = 408 if result.get("status") == "timeout" else 200
    return aiohttp.web.json_response(result, status=status_code)


# --- server }}}
