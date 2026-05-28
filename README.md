# comfyui-jupyter

<img width="1808" height="936" alt="Image" src="https://github.com/user-attachments/assets/e5c5f868-dca3-4f05-86b4-f211dfc2eb9c" />

- ComfyUIとJupyterの間でオブジェクトのやりとり
- ComfyUIプロセスの"生きた"オブジェクトをJupyter側で対話的に分析できる
- 任意のPython関数をカスタムノードとして実行できる
  - Jupyter kernel上の関数も指定可能
- Jupyterのタブ補完、グラフ表示、動的ウィジェットも動く
- Jupyter Notebook/Labのほか、VSCode/Cursorの`# %%`セル実行でも使える


## インストール

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/nat-chan/comfyui-jupyter
cd comfyui-jupyter

/path/to/ComfyUI/venv/bin/pip install -r requirements.txt

# kernel.json配置スクリプト
/path/to/ComfyUI/venv/bin/python scripts/install_kernel.py
```

## カスタムノード

### Jupyter Save

ComfyUI側のワークフローを流れる任意のオブジェクトをJupyter側の変数へと代入します。

- 入力
  - `key`: 保存先の変数名
  - `value`: 保存するオブジェクト (テンソル、PIL.Image、dict、何でも)

### Jupyter Load

Jupyter側で定義された任意の変数をComfyUIから読み出せます。

- 入力
  - `key`: 読み出す変数名
- 出力
  - `value`: 保存されていた値、無ければ `None`

### Jupyter Function

ComfyUIから任意の Python 関数を呼ぶ。位置引数とキーワード引数を動的なソケットで指定できます。

- 入力
  - `func_src`: 関数の取得元
    - `jupyter kernel`: Jupyter kernelから取得
    - `embedded code`: `embedded_code`に直接書いたソースから取得
    - `from file`: 指定したファイルパスから取得
  - `func_name`: 呼ぶ関数名
  - `embedded_code`:
    - `func_src=embedded code` 選択時のみ表示。ここに直接ソースをかけます。
    - わざわざカスタムノード化するまでもないな、というスニペットを書く用。
  - `file_path`:
    - `func_src=from file` 選択時のみ表示。`/path/to/file.py`
  - -- arg name, blank is positonal --
  - `arg[0]`, `arg[1]`…:
    - 動的ソケット、関数への入力を表現しています。
    - 入力ノードをarg[n]へつなげるとarg[n+1]、と増えていきます。
    - ソケットの横に表示されるウィジェットを空にしておくと**位置引数**、入力すると **キーワード引数** として評価されます
- 出力
  - `retval`: 関数の返り値


### Jupyter Client ID

ブラウザの自分の WebSocket セッション ID を表示するだけノードです。

複数のタブでComfyUIを開いているとき、`tools.queue_prompt(sid=...)` でどのタブで実行するか選択するときに使います。


## tools

Jupyter kernelにはデフォルトで`tools`オブジェクトがexposedされています。

実験に便利な関数群を追加していっています。

## グラフ系ライブラリのインライン表示設定

JupyterLab 拡張を追加で入れなくても plotly / matplotlib がインライン表示されるような設定が入っています。

自分で対応する拡張を入れているなどの場合は環境変数で無効化できます。

```bash
export COMFYUI_JUPYTER_DISABLE_DEFAULTS=plotly,matplotlib
# または完全停止
export COMFYUI_JUPYTER_DISABLE_DEFAULTS=all
```
