"""
国立国会図書館サーチ（NDLサーチ / https://ndlsearch.ndl.go.jp）

KOSMOS のメタデータ取得をやめた分、書籍のカバレッジをここで埋める。
OpenAlex / Crossref は雑誌論文中心で単行本に弱く、日本語書籍は特に落ちる。

■ 利用条件（https://ndlsearch.ndl.go.jp/help/api）
  - 個人・非営利で収益を得ない利用は、利用申請 不要
  - 「NDLサーチのAPIを利用している」旨の明記が必要 → README に記載
  - 同時リクエスト数に制限あり。多重アクセスを避けること
    → http レイヤが逐次 + 2.0秒間隔を強制するので条件を満たす

■ 実機で確認した仕様（2026-07-26）
  - `mediatype` パラメータは **常に 0 件を返す**。新 NDLサーチでは機能しない。
    絶対に付けないこと。書籍への絞り込みは category を見てクライアント側で行う。
  - `any=` は再現率が高い代わりに精度が低い（「障害者 参政権」で無関係な紀要が上位に
    来る）。既定は `title=` を使い、広く当たりたいときだけ --loose で any に切り替える。
  - 並び順は関連度ではなく資料名の五十音順。関連度ソートは提供されていないので、
    書籍を集めたいときは多めに取ってから絞る。
  - category は複数付く（例: 図書+デジタル / 記事+紙+新聞）。図書判定は「図書」の有無。
"""

import re
import xml.etree.ElementTree as ET

from .. import http
from ..model import (Paper, norm_doi, norm_year, clean_author,
                     REPO_HOST_HINTS as _REPO_HOST_HINTS)

BASE = "https://ndlsearch.ndl.go.jp/api/opensearch"
NAME = "ndl"

MAX_PER_REQUEST = 100
HARD_LIMIT = 300          # --books のときは絞り込みで減るので上限を高めに
BOOK_OVERFETCH = 6        # 実測で図書は全体の約 1/6

CATEGORY_TYPE = {
    "図書": "book",
    "記事": "article",
    "雑誌": "journal",
    "新聞": "newspaper-article",
}


def _local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _collect_fields(item):
    """名前空間を無視して {ローカル名: [値,...]} に潰す。"""
    out = {}
    for child in item:
        name = _local(child.tag)
        text = (child.text or "").strip()
        if not text:
            continue
        out.setdefault(name, []).append(text)
    return out


def _see_also(item):
    """
    <rdfs:seeAlso rdf:resource="..."/> の URI を拾う。

    値が本文ではなく属性に入っているため _collect_fields では落ちる。
    ここに DOI や機関リポジトリ・CiNii の書誌ページが入っており、
    NDL 由来のレコードで OA 本文に到達する唯一の手がかりになることがある。
    """
    out = []
    for child in item:
        if _local(child.tag) != "seeAlso":
            continue
        for k, v in child.attrib.items():
            if _local(k) == "resource" and v:
                out.append(v.strip())
    return out


# 「掲載誌：社会学研究科年報 2018 p.107-108」のような注記から巻号ページを取る。
# NDL は論文の巻号ページを専用要素で返さないため、ここから拾うしかない。
_PAGE_RE = re.compile(r"p\.?\s*(\d+)\s*[-–~〜]\s*(\d+)")
_PAGE1_RE = re.compile(r"p\.?\s*(\d+)")


def _one(f, key):
    v = f.get(key)
    return v[0] if v else ""


def _best_description(f):
    """
    description は2種類入る:
      - RSS 標準の <description>（HTML 断片。書誌を並べただけで抄録ではない）
      - <dc:description>（実質的な注記）
    後者を優先し、無ければ前者をタグ除去して使う。
    """
    vals = f.get("description", [])
    for v in vals:
        if "<" not in v:
            return v.strip()
    return _strip_html(vals[0]) if vals else ""


def _to_paper(item):
    f = _collect_fields(item)

    title = _one(f, "title")
    if not title:
        return None

    volume = _one(f, "volume")
    if volume and volume not in title:
        title = f"{title} {volume}".strip()

    authors = [clean_author(c) for c in f.get("creator", [])]
    authors = [a for a in authors if a]

    year = norm_year(_one(f, "issued") or _one(f, "date"))

    cats = f.get("category", [])
    ptype = ""
    for c in cats:
        if c in CATEGORY_TYPE:
            ptype = CATEGORY_TYPE[c]
            break
    if not ptype:
        ptype = "book"

    isbn = ""
    for ident in f.get("identifier", []):
        digits = ident.replace("-", "")
        if digits.isdigit() and len(digits) in (10, 13):
            isbn = ident
            break

    link = _one(f, "link") or _one(f, "guid")
    ids = {"ndl": link}
    if isbn:
        ids["isbn"] = isbn

    note = _best_description(f)
    m = _PAGE_RE.search(note) or _PAGE1_RE.search(note)
    pages = ("-".join(m.groups()) if m and len(m.groups()) == 2
             else (m.group(1) if m else ""))

    doi, repo = "", ""
    for uri in _see_also(item):
        cand = norm_doi(uri)
        if cand and not doi:
            doi = cand
            continue
        low = uri.lower()
        if not repo and any(h in low for h in _REPO_HOST_HINTS):
            repo = uri

    return Paper(
        title=title,
        authors=authors,
        year=year,
        venue=_one(f, "publicationName") or _one(f, "seriesTitle") or _one(f, "publisher"),
        type=ptype,
        doi=doi,
        abstract=note,
        landing_url=link,
        volume=volume,
        pages=pages,
        isbn=isbn.replace("-", "") if isbn else "",
        repo_url=repo,
        sources=[NAME],
        ids={k: v for k, v in ids.items() if v},
    )


def _is_book(item):
    return any((c.text or "").strip() == "図書" for c in item
               if _local(c.tag) == "category")


def _page(query, count, idx, loose):
    params = {"cnt": count, "idx": idx}
    params["any" if loose else "title"] = query
    body, _ = http.get(http.build_url(BASE, params))
    if not body.strip():
        return []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    channel = root.find("channel")
    return list(channel.findall("item")) if channel is not None else []


def search(query, count=50, books_only=False, loose=False, **_ignored):
    """
    NDLサーチを検索する。

    books_only=True のときは category=図書 でクライアント側フィルタし、
    目標件数に届くよう多めに取得する（mediatype は使えないため）。
    """
    want = min(count, HARD_LIMIT)
    budget = min(want * BOOK_OVERFETCH, HARD_LIMIT * 2) if books_only else want

    out, idx, scanned = [], 1, 0
    while len(out) < want and scanned < budget:
        asked = min(MAX_PER_REQUEST, budget - scanned)
        items = _page(query, asked, idx, loose)
        if not items:
            break
        scanned += len(items)
        idx += len(items)
        for it in items:
            if books_only and not _is_book(it):
                continue
            p = _to_paper(it)
            if p:
                out.append(p)
            if len(out) >= want:
                break
        if len(items) < asked:
            break

    return out[:want]


def search_books(query, count=50):
    return search(query, count=count, books_only=True)
