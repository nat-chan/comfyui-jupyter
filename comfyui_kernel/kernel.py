"""ComfyUI Jupyter Kernel.

ComfyUI の /jupyter エンドポイントにコードを転送し、結果を Jupyter に返す。
ipykernel.kernelbase.Kernel をベースにしており、将来の matplotlib 等の
リッチ出力拡張にも対応しやすい構造になっている。
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

from ipykernel.kernelbase import Kernel


COMFYUI_DEFAULT_URL = "http://127.0.0.1:8188"


class ComfyUIKernel(Kernel):
    implementation = "comfyui"
    implementation_version = "0.1.0"
    language = "python"
    language_version = "3"
    language_info = {
        "name": "python",
        "mimetype": "text/x-python",
        "file_extension": ".py",
        "codemirror_mode": {"name": "ipython", "version": 3},
        "pygments_lexer": "ipython3",
    }
    banner = "ComfyUI Kernel - Execute Python with access to ComfyUI variables"

    @property
    def comfyui_url(self) -> str:
        """ComfyUI サーバーの URL。環境変数で変更可能。"""
        import os

        return os.environ.get("COMFYUI_URL", COMFYUI_DEFAULT_URL)

    def _post_comfyui(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        """ComfyUI エンドポイントに JSON POST して結果を受け取る。"""
        url = f"{self.comfyui_url}{path}"
        payload = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))

    def _send_to_comfyui(self, code: str) -> dict[str, Any]:
        """コード実行用。"""
        return self._post_comfyui("/jupyter_execute_code", {"code": code})

    def do_execute(
        self,
        code: str,
        silent: bool,
        store_history: bool = True,
        user_expressions: dict[str, Any] | None = None,
        allow_stdin: bool = False,
    ) -> dict[str, Any]:
        if not code.strip():
            return {
                "status": "ok",
                "execution_count": self.execution_count,
                "payload": [],
                "user_expressions": {},
            }

        try:
            result = self._send_to_comfyui(code)
        except Exception as e:
            if not silent:
                self.send_response(
                    self.iopub_socket,
                    "stream",
                    {"name": "stderr", "text": f"ComfyUI connection error: {e}\n"},
                )
            return {
                "status": "error",
                "execution_count": self.execution_count,
                "ename": type(e).__name__,
                "evalue": str(e),
                "traceback": [str(e)],
            }

        if not silent:
            # stdout
            stdout_text: str = result.get("stdout", "")
            if stdout_text:
                self.send_response(
                    self.iopub_socket,
                    "stream",
                    {"name": "stdout", "text": stdout_text},
                )

            # stderr
            stderr_text: str = result.get("stderr", "")
            if stderr_text:
                self.send_response(
                    self.iopub_socket,
                    "stream",
                    {"name": "stderr", "text": stderr_text},
                )

            # display() 経由のリッチ出力 (matplotlib 図、HTML 等)
            for dd in result.get("display_data", []):
                data: dict[str, Any] = dd[0] if isinstance(dd, (list, tuple)) else dd.get("data", dd)
                metadata: dict[str, Any] = dd[1] if isinstance(dd, (list, tuple)) and len(dd) > 1 else dd.get("metadata", {})
                self.send_response(
                    self.iopub_socket,
                    "display_data",
                    {"data": data, "metadata": metadata},
                )

            # 最後の式の結果 (MIME bundle)
            execute_result: dict[str, Any] | None = result.get("execute_result")
            if execute_result is not None:
                self.send_response(
                    self.iopub_socket,
                    "execute_result",
                    {
                        "execution_count": self.execution_count,
                        "data": execute_result.get("data", {}),
                        "metadata": execute_result.get("metadata", {}),
                    },
                )

            # エラー
            if result.get("status") == "error":
                traceback_list: list[str] = result.get("traceback", [])
                if isinstance(traceback_list, str):
                    traceback_list = traceback_list.splitlines()
                self.send_response(
                    self.iopub_socket,
                    "error",
                    {
                        "ename": result.get("ename", ""),
                        "evalue": result.get("evalue", ""),
                        "traceback": traceback_list,
                    },
                )

        if result.get("status") == "error":
            traceback_list_ret: list[str] = result.get("traceback", [])
            if isinstance(traceback_list_ret, str):
                traceback_list_ret = traceback_list_ret.splitlines()
            return {
                "status": "error",
                "execution_count": self.execution_count,
                "ename": result.get("ename", ""),
                "evalue": result.get("evalue", ""),
                "traceback": traceback_list_ret,
            }

        return {
            "status": "ok",
            "execution_count": self.execution_count,
            "payload": [],
            "user_expressions": {},
        }

    def do_is_complete(self, code: str) -> dict[str, str]:
        """セルの入力が完了しているか判定する。"""
        import ast

        try:
            ast.parse(code)
            return {"status": "complete"}
        except SyntaxError:
            # 不完全なコードの可能性
            if code.rstrip().endswith(":") or code.rstrip().endswith("\\"):
                return {"status": "incomplete", "indent": "    "}
            return {"status": "invalid"}

    def do_complete(self, code: str, cursor_pos: int) -> dict[str, Any]:
        """ComfyUI の InteractiveShell に補完を委譲する。"""
        result = self._post_comfyui(
            "/jupyter_complete",
            {"code": code, "cursor_pos": cursor_pos},
        )
        return {
            "status": "ok",
            "matches": result.get("matches", []),
            "cursor_start": result.get("cursor_start", cursor_pos),
            "cursor_end": result.get("cursor_end", cursor_pos),
            "metadata": {},
        }
