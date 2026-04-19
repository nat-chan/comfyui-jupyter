import re
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

M: dict = {}


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
        global M
        M[key] = value
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
        global M
        return (M[key],)


# --- node }}}

# {{{ server ---
routes: aiohttp.web_routedef.RouteTableDef = PromptServer.instance.routes
app: aiohttp.web_app.Application = PromptServer.instance.app


@routes.post("/jupyter")
async def jupyter(request):
    data = await request.json()
    return aiohttp.web.json_response(data)


# --- server }}}
