### カスタムノード名

Jupyter Function

### 入力

1. func src: 以下の2つから選択
  - jupyter kernel
  - embedded code
2. embedded code: string
  - func srcがjupyter kernelの時は表示されず、embedded codeの時に表示されるmultilineのtext box
3. func name: string
  - func srcがjupyter kernelの時に表示されて、embedded codeの時に表示されない1行のtext box
4. args[0]: 任意の型の入力を受け付ける
  - 入力ノードを接続すると、"Bind args[0] to"というラベルで1行のテキスト入力がウィジェットに追加される
    - 文字列が入力されると、ソケットのargs[0]という表示が入力した文字列に変わる。後続の入力の名前もargs[1] -> args[0]のように整合性を保つ
    - 入力文字列が何か入力されている状態からブランクに戻った時、この入力名もargs[0]に戻り、後続の入力名もargs[0] -> args[1]のように整合性を保つ
  - 同様に入力ノードを接続したタイミングで、args[1]という名前の入力がウィジェットに追加される
5. args[1]...
  - 以降も同様のロジックで接続するノードが増えるたびに変数名を表すテキストボックスと追加の入力ができ、途中のノードの接続が外れると、整合性を保つようにする

### 出力

1. retval: 任意の型につなげられるようにする

### 機能

- 関数実体の作成
  - func srcがjupyter kernelのとき
    - `_user_ns`からfunc nameで指定された関数名を探し、これを関数実体とする
  - func srcがembedded codeのとき
    - embedded code入力をast構文解析し、rootにただ一つ関数が定義されているとして評価する。そしてこれを関数実体とする
      - 注: placeholderは関数名fだが、任意の名前で関数名が定義されていても大丈夫なようにする
- 引数の組み立て
  - 以降の入力はany型1、string型1、any型2、string型2…と交互に並んでいて、2の倍数こあるはず
  - string型の入力がブランクでなければ、対応するany型の入力をキーワード入力の引数として扱う、変数名はstring型の入力
  - string型の入力がブランクなら、対応するany型の入力を位置引数として扱う
- 関数実行と値の返却
  - 以上のロジックで得られた関数に実際に引数を使って評価し、返値をカスタムノードの出力とする
