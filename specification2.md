### カスタムノード名

Jupyter Function

### 入力

#### 1. func src (combo, dropdown)

以下の2つから選択する。選択値に応じて表示される子入力が切り替わる(V3 `DynamicCombo`)。

- `jupyter kernel`: `func name` 子入力が表示される
- `embedded code`: `embedded code` 子入力が表示される

#### 2. func name (string, 1 行)

`func src` が `jupyter kernel` のときだけ表示される 1 行のテキスト入力。デフォルト値 `f`。

#### 3. embedded code (string, multiline)

`func src` が `embedded code` のときだけ表示される複数行テキスト入力。デフォルト値:

```python
def f(*args, **kwargs):
    return args, kwargs
```

placeholder は無し(Vue widget では描画されないため仕様から外した)。

#### 4. args[0]…args[n] (任意型, 動的)

任意の型を受け付けるソケット入力 (型 `*`)。動作:

- ノード作成直後は空の `args[0]` ソケット 1 個と、それに対応する 1 行テキスト widget(label `Bind args[0] to`)が並ぶ
- ユーザがソケットに上流ノードを接続するたびに、新しい末尾の空 pair (socket + widget) が自動追加される
- ソケットを切断したとき:
  - そのスロットが**末尾**ならそのまま末尾の空スロットとして残る
  - **中間**なら socket と widget の両方を削除し、後続を詰めて再採番する

##### スロット表示の規則 (positional 番号付け)

ソケットラベルと widget ラベルは **位置引数番号** に基づいて再計算される(スロット内部 index ではない)。

各スロットを上から走査し:

- ペアの widget に文字列が入力されているとき (= キーワード引数): socket ラベル/widget ラベル ともに **入力された文字列** を表示
- 空のとき (= 位置引数): socket ラベルは `args[k]`、widget ラベルは `Bind args[k] to`、ここで `k` は **そのスロットより上にある未命名スロット数**

例: ソケット並びが上から `a, args[0], b, args[1]` のとき、対応 widget は順に `a, Bind args[0] to, b, Bind args[1] to`。これは `_build_call_args` が組み立てる Python の `*args` の順序と一致する。

任意の widget が編集されたら全スロットが即座に再採番される(positional 番号のシフトに追従)。

### 出力

#### retval (任意型)

関数の戻り値。socket 型 `*` で任意の下流ノードに接続できる。

### 機能

#### 関数実体の解決

- `func src` が `jupyter kernel` のとき
  - `_user_ns` から `func name` で指定された名前の callable を取り出し、関数実体とする
  - `func name` が空、または見つからない/callable でない場合は `ValueError`
- `func src` が `embedded code` のとき
  - `embedded code` を `ast.parse` で構文解析し、root に **ただ 1 つ** の関数定義 (`ast.FunctionDef`) があることを確認
  - `compile` + `exec` で **隔離されたローカル名前空間** に評価し、その関数オブジェクトを取り出す(関数名は任意で OK、placeholder の `f` でなくてもよい)
  - root に関数定義が 0 個または 2 個以上あれば `ValueError`
  - 隔離スコープなので `_user_ns` の変数は **参照できない**(これは仕様上の意図)

#### 引数の組み立て

実行時、`accept_all_inputs=True` により全 socket 値が `**kwargs` として `execute()` に渡る。

- スロット内部 index 順 (`arg_0, arg_1, …`) に走査
- 対応する `argname_i` widget の値が空ならその socket 値を **位置引数** として末尾追加
- 空でなければ widget の値をキーとして **キーワード引数** に登録
- 未接続のソケットは ComfyUI が prompt から省くため `kwargs` に現れない(個別フォールバック処理は不要)

#### 関数呼び出しと返却

- 解決した関数実体に対し `func(*positional, **keyword)` を呼ぶ
- 戻り値を `retval` 出力としてそのまま返す

### 実装上の補足

#### スキーマ

V3 `io.ComfyNode` ベース。`define_schema()` で:

- `inputs`: `io.DynamicCombo.Input("func_src", options=[…])` 1 個のみ宣言
- `outputs`: `io.AnyType.Output("retval")`
- `accept_all_inputs=True` で動的 socket をスキーマ外で受け取る

`func_name` と `embedded_code` は `DynamicCombo.Option` の子入力として宣言されており、`execute()` には `func_src: dict[str, t.Any]` という単一引数で `{"func_src": "<選択値>", "func_name": "...", "embedded_code": "..."}` の形で渡る。

#### フロントエンド (`web/jupyter_function.js`)

socket と widget の動的管理を担う:

- 内部識別子は安定させる: socket = `arg_0..arg_{N-1}`、widget = `argname_0..argname_{N-1}`(これらの index は `node.inputs` 配列の順序と一致)
- 表示ラベルだけが positional 番号で計算される (`slot.localized_name` と `widget.label`)
- 末尾の空 pair を常に 1 個維持(中間切断時は再採番、新規接続時は新規追加)
- ワークフロー保存・復元: `configure` をオーバーライドし、保存された `arg_*` socket 数に合わせて argname widget を `origConfigure` 実行前に事前生成、index ベースの `widgets_values` 復元と整合させる

#### 制限事項

- ソケットは LiteGraph の従来「左カラム」で描画され、schema-declared widgets の **上に** 表示される。Vue node renderer の制約上、動的に追加した socket を widget 領域に inline 配置することは現状できない(将来カスタム Vue widget を実装すれば可能)。
- 現状はスロット数の上限なし(プログラム的には `Number.parseInt` の範囲)。実用上 `node.inputs` の配列長が縦スクロールの限界。
