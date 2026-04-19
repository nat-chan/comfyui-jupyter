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
    banner = "ComfyUI Kernel - Execute Python with access to ComfyUI variables (M)"

    @property
    def comfyui_url(self) -> str:
        """ComfyUI サーバーの URL。環境変数で変更可能。"""
        import os

        return os.environ.get("COMFYUI_URL", COMFYUI_DEFAULT_URL)

    def _send_to_comfyui(self, code: str) -> dict[str, Any]:
        """ComfyUI /jupyter エンドポイントにコードを送信して実行結果を受け取る。"""
        url = f"{self.comfyui_url}/jupyter"
        payload = json.dumps({"code": code}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))

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
            # stdout を stream として送信
            stdout_text: str = result.get("stdout", "")
            if stdout_text:
                self.send_response(
                    self.iopub_socket,
                    "stream",
                    {"name": "stdout", "text": stdout_text},
                )

            # stderr を stream として送信
            stderr_text: str = result.get("stderr", "")
            if stderr_text:
                self.send_response(
                    self.iopub_socket,
                    "stream",
                    {"name": "stderr", "text": stderr_text},
                )

            # エラーの場合は traceback を送信
            if result.get("status") == "error":
                tb_text: str = result.get("traceback", "")
                self.send_response(
                    self.iopub_socket,
                    "error",
                    {
                        "ename": result.get("ename", ""),
                        "evalue": result.get("evalue", ""),
                        "traceback": tb_text.splitlines(),
                    },
                )

            # 最後の式の結果を execute_result として送信
            result_repr: str | None = result.get("result")
            if result_repr is not None:
                self.send_response(
                    self.iopub_socket,
                    "execute_result",
                    {
                        "execution_count": self.execution_count,
                        "data": {"text/plain": result_repr},
                        "metadata": {},
                    },
                )

        if result.get("status") == "error":
            return {
                "status": "error",
                "execution_count": self.execution_count,
                "ename": result.get("ename", ""),
                "evalue": result.get("evalue", ""),
                "traceback": result.get("traceback", "").splitlines(),
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
        """基本的な補完を提供する。将来的に ComfyUI 側の名前空間に基づく補完も可能。"""
        return {
            "status": "ok",
            "matches": [],
            "cursor_start": cursor_pos,
            "cursor_end": cursor_pos,
            "metadata": {},
        }
