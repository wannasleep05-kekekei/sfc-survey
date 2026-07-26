"""
KOSMOS（慶應義塾メディアセンター / Ex Libris Primo VE）

**このモジュールは HTTP リクエストを一切送信しません。** URL を組み立てるだけです。

慶應のサーバから見ると、生成された URL をブラウザで開く行為は、利用者が検索窓に
入力したのと区別がつきません。技術的にはブックマークと同じです。したがって
メディアセンターへの事前相談も、レート制限も、User-Agent の申告も必要ありません。
このツールが慶應のシステムに機械的に触れる箇所は、ゼロです。

■ Primo の検索は URL からは制御できない（2026-07-26 実測）
    KOSMOS に検索語を URL で渡すと、検索欄には正しく入るが、実行される検索は
    極端に緩い。実測値:
      - /discovery/search  title,contains,ノーマライゼーション → 27件、全て無関係
      - /discovery/jsearch any,contains,ノーマライゼーション   → 66,725件、
        しかも図書・ビデオが並び、雑誌に絞られていない
    したがって「機械が所蔵を判定する」ことはできない。このモジュールの役割は
    **正しい検索語を用意して、検索欄に入った状態で画面を開くところまで**。
    検索の実行と結果の判断は利用者が行う。

■ 意図的に実装していない機能
    - 内部 REST (primaws/rest/pub/pnxs) からの書誌取得
    - 慶應アカウントでのログイン（Okta SSO / JWT / cookie 流用）
    - 契約フルテキストの取得
    - 所蔵情報の自動収集

  最初の項目は無認証で叩けますが、公開 API として提供されているものではありません。
  無認証で応答することと、叩いてよいことは別です。所蔵確認は url() が生成した
  リンクをブラウザで開き、結果を `sfc-search hold` で記録してください。
"""

import re
import urllib.parse

NAME = "kosmos"

HOST = "https://search.lib.keio.ac.jp"
VID = "81SOKEI_KEIO:KEIO"      # 早慶共同運用ビュー（慶應）
SEARCH_UI = f"{HOST}/discovery/search"
JOURNAL_UI = f"{HOST}/discovery/jsearch"   # 雑誌検索。誌名のみを対象にする

RTYPES = ("books", "articles", "journals", "dissertations", "reviews",
          "newspaper_articles", "conference_proceedings", "reference_entrys")


def url(query, rtype=None, scope="all"):
    """KOSMOS の検索結果ページ URL を組み立てる。"""
    params = {
        "query": f"any,contains,{query}",
        "vid": VID,
        "tab": "Everything",
        "search_scope": "MyInstitution" if scope == "catalog" else "MyInst_and_CI",
        "lang": "ja",
    }
    if rtype:
        params["mfacet"] = f"rtype,include,{rtype},1"
    return SEARCH_UI + "?" + urllib.parse.urlencode(params)


# 掲載誌名の位置に入るが、実在の雑誌ではないラベル。
# OpenAlex などが機関リポジトリ収録物やアグリゲータ由来のレコードに付ける。
# これで KOSMOS を引いても意味がないので照会対象から外す。
_FAKE_VENUES = (
    "institutional repositories database", "irdb", "cinii", "ci.nii",
    "j-stage", "jstage", "doaj", "semantic scholar", "crossref",
    "researchgate", "figshare", "zenodo", "ssrn", "arxiv",
    "unknown", "n/a",
)


def is_real_venue(venue):
    """掲載誌名として KOSMOS に問い合わせる価値があるか。"""
    v = (venue or "").strip().lower()
    if len(v) < 2:
        return False
    return not any(bad in v for bad in _FAKE_VENUES)


def normalize_choon(t):
    """
    カナに続くハイフン類を長音符「ー」に直す。

    書誌データでは「ノ-マライゼ-ション」「法学セミナ-」のように、長音符が
    ASCII ハイフンや各種ダッシュで記録されていることが多い。そのまま KOSMOS に
    投げると当たらないため、カナの直後にあるものだけを長音符に寄せる。
    （「A-B」のような英数字間のハイフンは触らない）
    """
    return re.sub(r"(?<=[\u30a0-\u30ff\u3040-\u309f])[-\u2010-\u2015\uff0d\u2212]", "ー", t)


def clean_query(t, maxlen=24):
    """
    KOSMOS 用に検索語を短く刈り込む。

    Primo は日本語クエリを緩い OR で照合するため、語を渡せば渡すほど
    無関係な資料が上位に来る。実測では長い誌名・書名をそのまま投げると
    完全に的外れな結果しか返らなかった。短く保つことが唯一の対策。
    """
    if not t:
        return ""
    t = re.sub(r"<[^>]*>", " ", t)                    # <特集> 等のタグ
    t = re.sub(r"[(（\[［][^)）\]］]*[)）\]］]", " ", t)  # 括弧内を全部落とす
    t = re.split(r"\s*[:：]\s*", t)[0]                 # 副題を落とす
    t = re.split(r"\s*--\s*", t)[0]                    # --補足-- を落とす
    t = re.split(r"\s*[=＝]\s*", t)[0]                 # 並列書名を落とす
    t = re.sub(r"[「」『』\u3000]", " ", t)
    t = normalize_choon(t)
    t = re.sub(r"\s+", " ", t).strip(" .,;・")
    return t[:maxlen]


def journal_url(venue):
    """
    雑誌検索の画面を、検索欄に誌名が入った状態で開く URL。

    自動実行される検索結果は当てにならない（上記参照）。利用者が検索ボタンを
    押し直して判断することを前提にしている。
    """
    q = clean_query(venue)
    if not q:
        return ""
    return JOURNAL_UI + "?" + urllib.parse.urlencode({
        "vid": VID, "query": f"any,contains,{q}", "lang": "ja",
    })


def url_for(paper):
    """
    1件の所蔵を確認するための検索 URL。照会できない場合は空文字を返す。

    論文と書籍で引くものが違う:
      - 論文 → **掲載誌名**を雑誌検索で引く。慶應がその雑誌を持っているかが
        知りたいことなので、論文タイトルで蔵書検索しても意味がない。
      - 書籍 → 書名（+ 第一著者の姓）を図書facetで引く。

    DOI は使わない。KOSMOS の DOI 検索は当たりが悪く、上流メタデータで DOI が
    誤っている場合に無関係な資料へ飛ばしてしまう（実際に誤 DOI を確認済み）。
    """
    is_book = (paper.type or "").lower() in ("book", "book-chapter", "図書")

    if not is_book:
        if is_real_venue(paper.venue):
            return journal_url(paper.venue)
        return ""       # 誌名が無い/偽物 → 照会不能

    q = clean_query(paper.title)
    if not q:
        return ""
    if paper.authors:
        first = paper.authors[0].split(",")[0].strip()
        if first and len(first) <= 8:
            q = f"{q} {first}"
    return url(q, rtype="books")
