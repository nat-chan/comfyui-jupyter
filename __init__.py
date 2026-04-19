from __future__ import annotations

import ast
import io
import re
import sys
import traceback
import typing as t
from abc import ABCMeta

import aiohttp
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


def _execute_code(code: str) -> dict[str, t.Any]:
    """コードを _user_ns 内で実行し、stdout/stderr/結果を返す。

    最後のstatementが式(Expression)の場合、その値を result として返す。
    これにより IPython のような "最後の式の値を表示" を実現する。
    """
    global _user_ns

    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    result_value: t.Any = None
    status: str = "ok"
    ename: str = ""
    evalue: str = ""
    tb: str = ""

    # AST で最後の式を分離する
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # SyntaxError はそのまま実行側に渡して traceback を取得する
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stdout_capture, stderr_capture
        try:
            exec(compile(code, "<jupyter>", "exec"), _user_ns)
        except Exception as e:
            status = "error"
            ename = type(e).__name__
            evalue = str(e)
            tb = traceback.format_exc()
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr

        return {
            "status": status,
            "stdout": stdout_capture.getvalue(),
            "stderr": stderr_capture.getvalue(),
            "result": None,
            "ename": ename,
            "evalue": evalue,
            "traceback": tb,
        }

    # 最後の statement が Expr (式) なら分離して eval する
    last_expr_node: ast.Expr | None = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        node = tree.body.pop()
        assert isinstance(node, ast.Expr)
        last_expr_node = node

    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout_capture, stderr_capture
    try:
        # 前半を exec
        if tree.body:
            exec(compile(tree, "<jupyter>", "exec"), _user_ns)
        # 最後の式を eval
        if last_expr_node is not None:
            result_value = eval(  # noqa: S307
                compile(ast.Expression(body=last_expr_node.value), "<jupyter>", "eval"),
                _user_ns,
            )
    except Exception as e:
        status = "error"
        ename = type(e).__name__
        evalue = str(e)
        tb = traceback.format_exc()
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr

    return {
        "status": status,
        "stdout": stdout_capture.getvalue(),
        "stderr": stderr_capture.getvalue(),
        "result": repr(result_value) if result_value is not None else None,
        "ename": ename,
        "evalue": evalue,
        "traceback": tb,
    }


@routes.post("/jupyter")
async def jupyter(request: aiohttp.web.Request) -> aiohttp.web.Response:
    data = await request.json()
    code: str = data.get("code", "")
    result = _execute_code(code)
    return aiohttp.web.json_response(result)


# --- server }}}
