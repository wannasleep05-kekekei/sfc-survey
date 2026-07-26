# sfc-search-tools

SFC の研究会・卒プロ向け、先行研究サーベイ CLI。

**慶應のシステムに機械的なアクセスを一切行いません。** アカウント情報も扱いません。事前の許諾申請も不要です。

---

## 何をするツールか

先行研究サーベイのボトルネックは「PDF を集める速度」ではなく「**何を読むべきか見極める作業**」です。このツールはそこを加速します。

```
search    →  数百件のメタデータを機械で集める（公開APIのみ）
export    →  絞り込み用に書き出す
holdings  →  慶應の所蔵確認URLを出す（人がブラウザで確認）
open      →  絞った20〜30本を人間がブラウザで開く
ill       →  残りは文献複写依頼へ
```

## セットアップ

Python 3.8 以上。外部依存はありません。

```bash
git clone https://github.com/wannasleep05-kekekei/sfc-survey
cd sfc-survey
./sfc-search config --contact yourname@keio.jp
```

連絡先は User-Agent に載ります。何か問題があったときに、遮断ではなくあなたにメールが来るようにするためです。OpenAlex と Crossref はメールアドレス付きのリクエストを優先レーン（polite pool）で処理するので、速度面でも得をします。

## 使い方

### 横断検索

```bash
sfc-search search "視覚障害 情報アクセシビリティ" --count 100
```

OpenAlex・Crossref・CiNii・NDLサーチを叩いて、DOI とタイトルで名寄せして統合します。

```bash
--books                             # 書籍に絞る（NDL の category=図書）
--loose                             # NDL を全文検索に（再現率↑ 精度↓）
--source openalex --source ndl      # ソースを指定
--since 2015 --until 2024           # 年で絞る
--sort cited                        # 被引用数順
--verbose                           # 抄録も表示
```

### 引用チェイニング

サーベイの王道は「重要な1本を見つけて引用の前後に辿る」です。

```bash
sfc-search refs 10.1145/3234695        # この論文が引用している文献
sfc-search cited-by 10.1145/3234695    # この論文を引用している文献
sfc-search snowball 10.1145/3234695    # 前後1段をまとめて
```

既定は被引用数順です。`--sort new` で新しい順、`--sort old` で古い順。被引用数順だと分野の古典が上位に来る反面、最新の研究が下に沈むので、動向を見たいときは `--sort new` と併用してください。

### 慶應の所蔵確認

```bash
sfc-search holdings                    # 確認用URLの一覧
sfc-search holdings --open 1-10        # 10件をブラウザで開く
sfc-search hold 3 --library SFC --call 369.27@Y16
sfc-search hold 4 --none
```

`holdings` は **KOSMOS の検索 URL を組み立てるだけ**で、リクエストは送りません。実際のアクセスはあなたがブラウザで行います。結果を `hold` で記録しておくと `ill` の精度が上がります。

論文は掲載誌名、書籍は書名で照会します。誌名の長音記号のゆれ（`ノ-マライゼ-ション` → `ノーマライゼーション`）は自動で正規化し、同じ雑誌の論文はまとめて1件の照会にします。

### KOSMOS の検索は URL からは制御できません

実測（2026-07-26）で確認した挙動です。

| 投げたもの | 結果 |
|---|---|
| `/discovery/search` に `title,contains,ノーマライゼーション` | 27件、すべて無関係 |
| `/discovery/jsearch` に `any,contains,ノーマライゼーション` | 66,725件、図書やビデオが並ぶ |

Primo は URL 経由のクエリを極端に緩く照合します。**したがって、このツールが所蔵の有無を自動判定することはできません。**

`holdings` の役割は「正しい検索語を用意して、それが検索欄に入った状態で KOSMOS を開くところまで」です。開いたら検索ボタンを押し直してください。自動表示される結果は当てになりません。

検索語だけ欲しい場合は `sfc-search holdings --terms` でコピペ用に出力できます。

30本の確認で 10〜15 分ほどを見てください。

### 絞り込み

```bash
sfc-search export --format md --with-abstract --out survey.md
```

メタデータ＋抄録を書き出します。これを読んで精読すべきものを選びます。分類・仕分けを LLM に手伝わせる場合もこのファイルを渡してください。

### 本文の入手

```bash
sfc-search fetch --limit 30      # OA本文のみ自動取得
sfc-search open 3 7 12           # 残りをブラウザで開く
sfc-search ill                   # 所蔵なし・非OAを文献複写依頼形式で出力
```

`fetch` が取りに行くのは Unpaywall が OA と判定したものだけです。慶應が契約している有料フルテキストには触れません。

---

## 意図的に実装していない機能

| 機能 | 理由 |
|---|---|
| 慶應アカウントでのログイン | パスワードの平文保管と MFA の自動承認は、情報システム利用規程 第4条③④に抵触します（認証情報を一切扱わない設計です） |
| 契約フルテキストの取得 | 出版社ライセンスの systematic download 禁止条項に違反します。検知されると**大学全体**のアクセスが停止します（第4条⑨） |
| KOSMOS からの書誌自動取得 | `primaws/rest/pub/pnxs` は無認証で応答しますが、公開 API として提供されているものではありません。無認証で叩けることと、叩いてよいことは別です |
| 並列リクエスト | サーバへの負荷集中を避けるため |
| Google Scholar のスクレイピング | 公式 API がなく ToS で禁止されています（第4条⑪）。OpenAlex が上位互換です |
| User-Agent の偽装 | 虚偽の申告です（第4条⑪） |

これらは「今は作っていない」ではなく「作らない」機能です。Pull Request でも受け付けません。

契約フルテキストが必要な場合は `sfc-search open` でブラウザから正規にアクセスするか、`sfc-search ill` で文献複写依頼を出してください。大量のテキストを計量分析（TDM）に使いたい場合は、出版社の TDM 窓口を通す必要があります。メディアセンターに相談してください。

---

## データソース

| ソース | 用途 | アクセス |
|---|---|---|
| [OpenAlex](https://openalex.org) | 横断検索・引用グラフ | API（1.0秒間隔） |
| [Crossref](https://www.crossref.org) | DOI 書誌 | API（1.0秒間隔） |
| [CiNii Research](https://cir.nii.ac.jp) | 日本語論文 | API（2.0秒間隔） |
| [NDLサーチ](https://ndlsearch.ndl.go.jp) | 日本語書籍 | API（2.0秒間隔・書名検索） |
| [Unpaywall](https://unpaywall.org) | OA 判定 | API（1.0秒間隔） |
| KOSMOS | 慶應所蔵の確認 | **URL生成のみ・リクエストなし** |

間隔はホストごとに強制され、呼び出し側から短縮できません。並列実行はしません。

本ツールは国立国会図書館サーチの API を利用しています。

NDLサーチについて実機で確認した挙動（2026-07-26）:

- `mediatype` パラメータは常に0件を返すため使用していません。書籍の絞り込みは
  `category` を見てクライアント側で行っています。
- 既定では書名検索（`title=`）を使います。全文検索（`any=`）は再現率が高い反面
  精度が低く、無関係な紀要などが上位に来るため `--loose` 指定時のみ使います。
- 並び順は関連度ではなく資料名の五十音順です。`--books` では絞り込みで減る分を
  見込んで多めに取得しています。

---

## 準拠

- [慶應義塾情報システム利用規程](https://www.itc.keio.ac.jp/ja/information_security_prescript.html)
- 慶應義塾大学メディアセンター 電子リソース利用条件
- [NDLサーチ API 利用条件](https://ndlsearch.ndl.go.jp/help/api)（個人・非営利利用のため利用申請は不要。継続利用時は申請フォームから連絡することが推奨されています）
- 各 API の利用規約（OpenAlex / Crossref / CiNii / Unpaywall）

取得した文献の再配布はしないでください。各自の研究利用の範囲で。

## ライセンス

MIT
