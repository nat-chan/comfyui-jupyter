# comfyui-jupyter

ComfyUI のプロセス内で動く対話的 Python Jupyter カーネルと、ComfyUI ワークフローと
Jupyter ノートを橋渡しするカスタムノード群。

## インストール

ComfyUI 本体の venv をそのまま使う。独立した venv は不要。

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/nat-chan/comfyui-jupyter
cd comfyui-jupyter

# ComfyUI venv へ依存を入れる
/path/to/ComfyUI/venv/bin/pip install -r requirements.txt

# Jupyter カーネル登録 (sys.executable と repo パスが kernel.json に焼かれる)
/path/to/ComfyUI/venv/bin/python scripts/install_kernel.py
```

これで `~/.local/share/jupyter/kernels/comfyui/kernel.json` が生成され、JupyterLab 起動時
に "ComfyUI (Python)" カーネルが選択できるようになる。ComfyUI を再起動すると custom_node
として読み込まれ、`/comfyui_jupyter/proxy` WebSocket エンドポイントが立ち上がる。

リポジトリを移動したり ComfyUI venv を切り替えた場合は `install_kernel.py` を再実行する。

### 動作要件

- ComfyUI 本体 (Nodes 2.0 を有効にすると `JupyterFunction` がインラインソケットで描画される)
- JupyterLab (任意の場所にインストール可、kernel が見える環境であれば良い)
- Python 3.10+

### JupyterLab 側

JupyterLab はユーザが好きな方法でインストールしてよい。ComfyUI venv に同居させても、
別環境にしてもよい。kernelspec は OS ユーザ単位で登録されるので、いずれの JupyterLab
からも "ComfyUI (Python)" として認識される。

## カスタムノード

すべて `comfyui-jupyter` カテゴリ。

### Jupyter Save

ワークフローの中間値を kernel と共有する名前空間に保存する。

- 入力 `key` (string): 保存先のキー名
- 入力 `value` (任意): 保存する値 (テンソル、PIL.Image、dict、何でも)
- 出力: なし (output node)

保存後は Jupyter ノート側で `_user_ns[key]` 相当の場所から取り出せる
(実装上は ComfyUI プロセス内のモジュールレベル dict)。

### Jupyter Load

保存済みの値を取り出して下流ノードに流す。

- 入力 `key` (string): 取り出すキー名
- 出力 `value` (任意): 保存されていた値、無ければ `None`
- 常に再実行される (`fingerprint_inputs` が NaN を返すため)

### Jupyter Function

ワークフローから任意の Python 関数を呼ぶ。位置引数とキーワード引数を動的なソケットで指定。

- 入力 `func_src` (combo): 関数の取得元
  - `jupyter kernel`: kernel 内に定義された関数を `func_name` で参照
  - `embedded code`: ノードに直接書いたソースを評価し、その中の `func_name` 関数を取得
  - `from file`: 指定したファイルパスのソースを評価し、その中の `func_name` 関数を取得
- 入力 `func_name` (string): 呼ぶ関数の名前 (全モードで表示)
- 入力 `embedded_code` (string, multiline): `embedded code` 選択時のみ表示。Python ソース
- 入力 `file_path` (string): `from file` 選択時のみ表示。`/path/to/file.py`
- 入力 `arg_0`, `arg_1`, ... (動的ソケット, 任意型): 関数に渡す引数。
  対応する `argname_<i>` ウィジェットを空にしておくと**位置引数**、入力すると **キーワード
  引数** として渡される
- 出力 `retval` (任意): 関数の戻り値

毎回再実行されるので、`from file` で開いたファイルや kernel 内の関数定義を編集しても自動で反映される。

### Jupyter Client ID

ブラウザの自分の WebSocket セッション ID を表示するだけの仮想ノード。
`tools.queue_prompt(sid=...)` でブラウザを特定するときの参照用。
ワークフローには含まれない (`isVirtualNode`)。

## Jupyter 側 API

kernel の名前空間には以下が自動で injection される。

### `tools.queue_prompt(sid=None) -> str`

接続中のブラウザ (`sid` 省略時は全クライアントブロードキャスト) で
キューに登録された現在のワークフローを実行させ、`prompt_id` を返す。
ブラウザ側 JS でインターセプトして得た `prompt_id` をコールバックで受け取る仕組み。

### `tools.wait_prompt(prompt_id, timeout=600.0) -> dict`

`prompt_id` の実行が `PromptQueue.history` に書き込まれるまで待機し、
履歴 dict (status, outputs 等) を返す。タイムアウト時は `{"status": "timeout"}`。

### `tools.list_sids() -> list[str]`

現在 ComfyUI に WebSocket 接続中のブラウザ sid 一覧。
`tools.queue_prompt(sid=...)` の引数指定に使う。

## 自動デフォルト上書き

JupyterLab 拡張を追加で入れなくても plotly / matplotlib がインライン表示されるよう、
ComfyUI 起動時に環境変数を `setdefault` する。

- `PLOTLY_RENDERER=notebook_connected` (plotly が `application/vnd.plotly.v1+json` のみ
  ではなく CDN 付き `text/html` を出すようになる)
- `MPLBACKEND=module://matplotlib_inline.backend_inline` (`plt.show()` が PNG の
  `display_data` を出すようになる)

自分で対応する拡張を入れている (`jupyterlab-plotly` 等) 場合は環境変数

```bash
export COMFYUI_JUPYTER_DISABLE_DEFAULTS=plotly,matplotlib
# または完全停止
export COMFYUI_JUPYTER_DISABLE_DEFAULTS=all
```

または個別ライブラリのネイティブ env var (`PLOTLY_RENDERER` を自分で設定する等) で
override できる。新しい上書きを増やすには `__init__.py` の `_DEFAULTS` リストに 1 行
追加するだけ。
