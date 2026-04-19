from __future__ import annotations

import base64
import io
import re
import typing as t
from abc import ABCMeta

import aiohttp
from IPython.core.interactiveshell import InteractiveShell
from IPython.utils.capture import capture_output
from server import PromptServer  # noqa

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]


"""
https://zenn.dev/4kk11/articles/4e36fc68293bd2
https://github.com/chrisgoringe/Comfy-Custom-Node-How-To/wiki/api
"""

# コード実行間で変数を保持する名前空間
_user_ns: dict[str, t.Any] = {}


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
routes: aiohttp.web_routedef.RouteTableDef = PromptServer.instance.routes
app: aiohttp.web_app.Application = PromptServer.instance.app


import IPython.core.page as _page_mod  # noqa: E402


def _no_pager(strng: str | dict[str, str], start: int = 0, screen_lines: int = 0, pager_cmd: str | None = None) -> None:  # noqa: E501
    """ページャーの代わりに stdout に直接出力する。"""
    if isinstance(strng, dict):
        strng = strng.get("text/plain", "")
    print(strng)


_page_mod.page = _no_pager  # type: ignore[assignment]
_page_mod.display_page = _no_pager  # type: ignore[assignment]

_shell: InteractiveShell = InteractiveShell.instance(user_ns=_user_ns)
_OUT_RE: re.Pattern[str] = re.compile(r"^Out\[\d+\]: .*\n?", re.MULTILINE)


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
        figs.append((
            {"image/png": png_b64, "text/plain": repr(fig)},
            {},
        ))
    plt.close("all")
    return figs


def _execute_code(code: str) -> dict[str, t.Any]:
    """InteractiveShell でコードを実行し、リッチ出力を含む結果を返す。"""
    with capture_output(stdout=True, stderr=True, display=True) as captured:
        result = _shell.run_cell(code, silent=False, store_history=True)

    # display() 経由の出力を MIME bundle リストに変換
    display_data: list[t.Any] = [
        o._repr_mimebundle_() for o in captured.outputs  # type: ignore[union-attr]
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


@routes.post("/jupyter_execute_code")
async def jupyter_execute_code(request: aiohttp.web.Request) -> aiohttp.web.Response:
    data = await request.json()
    code: str = data.get("code", "")
    result = _execute_code(code)
    return aiohttp.web.json_response(result)


@routes.post("/jupyter_complete")
async def jupyter_complete(request: aiohttp.web.Request) -> aiohttp.web.Response:
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
    else:
        matches = []
        start = cursor_pos
        end = cursor_pos
    return aiohttp.web.json_response(
        {"matches": matches, "cursor_start": start, "cursor_end": end},
    )


# --- server }}}
