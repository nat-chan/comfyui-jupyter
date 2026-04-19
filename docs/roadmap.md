# ComfyUI Jupyter Kernel - Roadmap

## Architecture

ComfyUI (aiohttp, port 8188) 内の `InteractiveShell` でコードを実行し、別プロセスの Jupyter kernel が HTTP で中継する構成。

```
Jupyter Notebook/Lab
  └─ ComfyUIKernel (ipykernel.kernelbase.Kernel)
       └─ HTTP POST ─→ ComfyUI :8188
            ├─ /jupyter_execute_code  ... コード実行
            ├─ /jupyter_complete     ... タブ補完
            └─ (今後追加)
```

## Implemented

| Feature | Endpoint | Description |
|---------|----------|-------------|
| Code execution | `/jupyter_execute_code` | `InteractiveShell.run_cell()` + リッチ出力 (MIME bundle) |
| Tab completion | `/jupyter_complete` | `InteractiveShell.complete()` (Jedi ベース) |
| matplotlib capture | (execute 内) | `plt.savefig()` → base64 PNG 自動キャプチャ |
| Magic commands | (execute 内) | `%timeit`, `%ls`, `%%time` 等 |
| `?object` help | (execute 内) | ページャー無効化、stdout に直接出力 |
| JupyterSave / JupyterLoad | ComfyUI nodes | ComfyUI ワークフローと `_user_ns` 間で変数を共有 |
| `do_is_complete` | (kernel 内) | セル入力の構文検証 |

## Roadmap

### Priority 1 - Low effort, high impact

| Feature | User experience | Implementation |
|---------|----------------|----------------|
| **`do_inspect`** (Shift+Tab) | カーソル上のオブジェクトのドキュメント・シグネチャをポップアップ表示 | `/jupyter_inspect` を追加、`_shell.object_inspect_mime()` を呼ぶだけ |
| **`do_shutdown`** | カーネル終了時のクリーンアップ | `{"status": "ok"}` を返すだけ |
| **`do_clear`** | 名前空間リセット (`%reset` 相当) | `/jupyter_clear` を追加、`_shell.reset(False)` を呼ぶ |

### Priority 2 - Medium effort, moderate impact

| Feature | User experience | Implementation |
|---------|----------------|----------------|
| **`do_history`** | 上矢印で過去のセル履歴ブラウズ | `/jupyter_history` を追加、`_shell.history_manager` を公開。ただしフロントエンドがあまり使わない |
| **`user_expressions`** | `do_execute` 時に任意の式を追加評価して返す | `/jupyter_execute_code` のレスポンスに含める。実用上ほぼ使われない |
| **Experimental completions** | 補完候補に型情報・シグネチャを付与 | `_shell.Completer.completions()` (Jedi) を使用、`provisionalcompleter()` が必要 |

### Priority 3 - High effort, HTTP bridge の制約あり

| Feature | User experience | Blocker |
|---------|----------------|---------|
| **`do_debug_request`** | ブレークポイント・ステップ実行 | debugpy + DAP プロトコルが必要。HTTP ブリッジと根本的に相性が悪い |
| **comm protocol** (`comm_open` / `comm_msg` / `comm_close`) | ipywidgets (スライダー、ボタン等の対話的ウィジェット) | 双方向メッセージングが必要。HTTP では不可能、WebSocket への移行が前提 |
| **`input()` / `getpass`** | セル内で入力プロンプトを表示し、ユーザー入力を受け取る | stdin チャネルの双方向通信が必要 |
| **実行中断 (Ctrl+C)** | 実行中のセルをキャンセル | HTTP リクエスト中のシグナル送信が必要。ComfyUI 側にキャンセル機構が必要 |
| **Kernel subshells** | 複数の独立した実行コンテキスト | ipykernel 7+ の機能。HTTP ブリッジではスレッド/プロセス分離が困難 |

### Not planned

| Feature | Reason |
|---------|--------|
| `do_apply` | ipyparallel 用のレガシー機能。標準 notebook では不使用 |
| async/await native support | `InteractiveShell.run_cell()` が同期実行のため。`await` は magic 経由で部分的に動作 |
