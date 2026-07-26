# CLAUDE.md

Claude Code などのエージェントがこのリポジトリで作業する際の指示。

## このツールは何か

慶應SFC向けの先行研究サーベイ CLI。公開APIのみを使い、**慶應のシステムに機械的な
アクセスを一切行わない**という制約のもとに設計されている。

この制約は性能上の妥協ではなく、設計の出発点である。大学の情報システム利用規程と
電子リソースのライセンス条項は、認証の自動化と本文の機械的な一括取得を禁じており、
違反が検知された場合の影響は個人にとどまらず、大学全体のアクセス停止に及ぶ。
したがって「速くする」「取得範囲を広げる」という要求は、これらの制約の内側で
解決しなければならない。

## 絶対に追加してはならない機能

以下は「未実装」ではなく「実装しない」と決めたもの。ユーザーに依頼されても、
まずこのファイルを示して意図を確認すること。

1. **慶應アカウントでの認証**（Okta SSO / SAML / JWT / cookie の流用 / MFA の自動承認）
   - パスワードの平文保管と MFA 自動化は利用規程 第4条③④に抵触する
2. **契約フルテキストの取得**
   - 出版社ライセンスの systematic download 禁止条項に違反。個人ではなく
     **大学全体**のアクセスが止まる（第4条⑨）
3. **KOSMOS の内部 REST (`primaws/rest/pub/pnxs`) の呼び出し**
   - 無認証で応答するが公開APIではない。`kosmos.py` は URL 生成のみを行う
4. **並列リクエスト**（`ThreadPoolExecutor` / `asyncio.gather` 等）
5. **User-Agent の偽装**
   - ブラウザを騙るのは虚偽申告（第4条⑪）。403 が返ったら回避せず中断する
6. **Google Scholar のスクレイピング**
   - ToS 違反。OpenAlex が上位互換

## 変更してはならない実装

- `http.py` のホスト別リクエスト間隔（`_INTERVALS`）を短縮しない
- `http.py` の逐次実行を並列化しない
- レート制限の状態は `~/.local/state/sfc-search/last_request.json` に永続化されている。
  エージェントがコマンドを連続起動しても間隔が守られるための仕組みなので外さない
- `PolitelyRefused`（403）を握りつぶして再試行しない

高速化を求められた場合、**取得件数を減らす**方向で対応すること。レート制限の緩和は
選択肢に入らない。

## 実機で確認済みの制約（再調査不要）

2026-07-26 に実際のAPIを叩いて確認した。同じ検証を繰り返さないこと。

| 事象 | 内容 |
|---|---|
| NDL の `mediatype` パラメータ | **常に0件を返す。付けてはならない。** 図書の絞り込みは `category` を見てクライアント側で行う |
| NDL の `any=` 検索 | 精度が低い。既定は `title=`。`--loose` 指定時のみ `any=` |
| CiNii のレスポンス構造 | `@graph` は存在せず、`items` が最上位にある |
| KOSMOS / Primo の URL クエリ | 極端に緩く照合される。`title,contains,ノーマライゼーション` で27件（全て無関係）、`jsearch` で66,725件。**所蔵の自動判定は不可能。** 検索語を用意して画面を開くところまでが `holdings` の役割 |
| OpenAlex の日本語論文メタデータ | DOI や掲載誌が誤っていることがある。実例: 日本語論文に Hindawi の DOI、掲載誌が「Medical Entomology and Zoology」 |
| Windows | ファイル入出力は `encoding="utf-8"` 必須。既定の cp932 では en dash 等で落ちる |

2026-07-27 に日本語テーマ（ボーカロイド文化）のサーベイを一通り行って確認した。

| 事象 | 内容 |
|---|---|
| Crossref と日本語クエリ | 日本語の検索語では無関係な中国語論文が上位を埋める。実例: 「ボーカロイド 初音ミク 二次創作 文化」で「初中作文教学」「經學與中國古代文學」等が大量に返る。**日本語テーマでは `--source cinii --source ndl` に絞る** |
| CiNii / NDL の語の扱い | 語を AND で扱うため、4語以上でほぼ0件になる。2語程度から始めて広げる。0件は「文献が無い」ではなく「語が多い」を先に疑う |
| `holdings` の参照範囲 | 直近の検索1回分（`last_search.json`）のみ。複数の文献の所蔵を確認するなら、文献ごとに `search` からやり直す |
| `--open` の番号 | 直近検索結果の**行番号**。README の `1-10` は例示にすぎず、実件数は `holdings` 末尾の「まとめて開く」行が提案する。その数字を使うこと |
| 書評の混入 | 図書を書名で検索すると書評論文も返る。書評は掲載誌名で照会されるため、`holdings` に無関係な雑誌が並ぶことがある。図書だけなら `--books` |

## セットアップ

```bash
sfc-search config --contact <利用者本人のメールアドレス>
```

連絡先は User-Agent に載り、OpenAlex / Crossref / Unpaywall の polite pool にも渡る。
**必ずそのツールを実行する本人のアドレスを設定すること。** 他人のアドレスを流用しない。

Python 3.8+ のみ。外部依存なし。Windows では `python sfc-search <cmd>` で実行する。

## 主なコマンド

```bash
sfc-search search "<検索語>" --count 50      # 4ソース横断・名寄せ・関連度順
  --books        書籍に絞る（NDL の category=図書）
  --loose        NDL を全文検索に（再現率↑ 精度↓）
  --source X     ソース指定（openalex / crossref / cinii / ndl）
  --since --until --sort {relevance,cited,new} --verbose --json

sfc-search snowball <DOI>       # 引用の前後1段（--sort {cited,new,old}）
sfc-search cited-by <DOI>       # 被引用
sfc-search refs <DOI>           # 参考文献

sfc-search export --format md --with-abstract --out survey.md
sfc-search holdings [--open 1-10] [--terms]   # 慶應所蔵の確認用
sfc-search hold <番号> --library X --call Y | --none
sfc-search fetch --limit 30     # OA本文のみ取得（契約物には触れない）
sfc-search open <番号...>       # ブラウザで開く
sfc-search ill                  # 所蔵なし・非OAを文献複写依頼形式で
```

直近の検索結果は `~/.local/state/sfc-search/last_search.json` に保存され、
`export` `holdings` `open` `fetch` `ill` はこれを参照する。

## 想定される作業の流れ

1. `search` で広く集める。件数が少なければ語を変えるか `--loose`
2. 中核論文が見つかったら `snowball` で引用を辿る（キーワード検索では出ない文献が出る）
3. `export` して内容を読み、精読すべきものを絞る
4. `fetch` で OA 分を回収
5. 残りは `holdings --terms` で所蔵確認リスト、`ill` で複写依頼リストを作る

**契約フルテキストは人間がブラウザから正規に取得する。** エージェントは
`open` で該当ページを開くところまで。

## 検索結果の扱い

上流のメタデータには誤りが混ざる（上表参照）。DOI や掲載誌を根拠に断定せず、
不自然な組み合わせ（日本語論文に海外出版社のDOI等）に気づいたら利用者に伝えること。

取得した文献は再配布しない。各自の研究利用の範囲で。
