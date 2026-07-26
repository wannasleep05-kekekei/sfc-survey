"""
CiNii Research (https://cir.nii.ac.jp)

NII が公開している OpenSearch API を使う。日本語論文・紀要・学会誌のカバレッジは
OpenAlex や Crossref より明確に強く、SFC の日本語文献サーベイでは外せない。

認証は一切使わない。旧実装にあった慶應学認の自動ログインは削除済み。

■ 実機で確認した仕様（2026-07-27）
  - 論文には `prism:volume` / `prism:number` / `prism:startingPage` /
    `prism:endingPage` が入る。**ILL に出せる形にするにはこれが必須**。
  - 図書には `prism:publicationName` が無く、代わりに `dc:publisher`。
  - `dc:identifier` は型付きで NCID / ISBN / NAID / URI を返す。
    NCID があれば CiNii Books の所蔵ページ（国内所蔵館一覧）を引ける。
  - `dc:identifier` / `dc:source` の URI に機関リポジトリの書誌ページが入る。
    Unpaywall が JaLC DOI をほぼ拾えないため、**OA 本文への唯一の手がかりが
    ここになる**ことが多い（oa.resolve が使う）。
  - CiNii Books の OpenSearch API (ci.nii.ac.jp/books/opensearch) は appid 無しで
    403。回避せず、所蔵確認は URL を組んで人が開く（kosmos.cinii_books_url）。
"""

from .. import http
from ..model import (Paper, norm_doi, norm_year, clean_author,
                     REPO_HOST_HINTS as _REPO_HOST_HINTS)

BASE = "https://cir.nii.ac.jp/opensearch/all"
NAME = "cinii"


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _text(v):
    """CiNii は {"@value": "...", "@language": "ja"} 形式を返すことがある。"""
    if isinstance(v, dict):
        return (v.get("@value") or "").strip()
    if isinstance(v, list):
        for x in v:
            t = _text(x)
            if t:
                return t
        return ""
    return (v or "").strip() if isinstance(v, str) else ""


def _extract_items(data):
    """
    文献レコードの取り出し。CiNii Research は最上位に items を置くが、
    旧 CiNii Articles 形式（@graph[].items）も来る可能性があるため両対応にする。
    実機確認（2026-07-26）: 最上位 items のみ、@graph は存在しない。
    """
    if not isinstance(data, dict):
        return []
    items = _as_list(data.get("items"))
    if items:
        return items
    for g in _as_list(data.get("@graph")):
        if isinstance(g, dict):
            items.extend(_as_list(g.get("items")))
    return items


def _identifiers(item):
    """
    dc:identifier を型付きで拾う。

    CiNii Research は {"@type": "cir:NCID", "@value": "BC02624402"} の形で
    NCID・ISBN・NAID・URI を返す。旧実装は DOI 以外を捨てていたが、
    NCID は国内所蔵（CiNii Books）の照会キー、URI は OA 本文への入口になる。
    """
    out = {"doi": "", "ncid": "", "isbn": "", "uris": []}
    for ident in _as_list(item.get("dc:identifier")):
        if isinstance(ident, dict):
            kind = (ident.get("@type") or "").split(":")[-1].upper()
            val = _text(ident.get("@value"))
        else:
            kind, val = "", _text(ident)
        if not val:
            continue
        if kind == "NCID":
            out["ncid"] = out["ncid"] or val
        elif kind == "ISBN":
            out["isbn"] = out["isbn"] or val.replace("-", "")
        elif kind == "URI":
            out["uris"].append(val)
        else:
            out["doi"] = out["doi"] or norm_doi(val)
    for src in _as_list(item.get("dc:source")):
        if isinstance(src, dict) and src.get("@id"):
            out["uris"].append(src["@id"])
    return out


def _pick_repo_url(uris):
    """OA 本文が期待できる書誌ページを1つ選ぶ。無ければ空文字。"""
    for u in uris:
        low = u.lower()
        if any(h in low for h in _REPO_HOST_HINTS):
            return u
    return ""


def _to_paper(item):
    title = _text(item.get("dc:title") or item.get("title"))
    if not title:
        return None

    authors = []
    for c in _as_list(item.get("dc:creator")):
        name = clean_author(_text(c))
        if name:
            authors.append(name)

    # 図書には prism:publicationName が付かない。出版社で埋めないと venue が空になり、
    # 所蔵照会でも書き出しでも「不明」になってしまう。
    venue = (_text(item.get("prism:publicationName"))
             or _text(item.get("dc:publisher")))
    year = norm_year(_text(item.get("prism:publicationDate"))
                     or _text(item.get("dc:date")))

    landing = item.get("@id") or _text(item.get("link"))
    if isinstance(landing, dict):
        landing = landing.get("@id", "")

    ident = _identifiers(item)
    doi = ident["doi"] or norm_doi(_text(item.get("prism:doi")))

    start = _text(item.get("prism:startingPage"))
    end = _text(item.get("prism:endingPage"))
    pages = f"{start}-{end}" if start and end else (start or "")

    ids = {"cinii": landing or ""}
    if ident["ncid"]:
        ids["ncid"] = ident["ncid"]
    if ident["isbn"]:
        ids["isbn"] = ident["isbn"]

    return Paper(
        title=title,
        authors=authors,
        year=year,
        venue=venue,
        type=_text(item.get("dc:type")) or "article",
        doi=doi,
        abstract=_text(item.get("dc:description")),
        landing_url=landing or "",
        volume=_text(item.get("prism:volume")),
        issue=_text(item.get("prism:number")),
        pages=pages,
        isbn=ident["isbn"],
        ncid=ident["ncid"],
        repo_url=_pick_repo_url(ident["uris"]),
        sources=[NAME],
        ids={k: v for k, v in ids.items() if v},
    )


def search(query, count=50, since=None, until=None):
    out, start = [], 1
    while len(out) < count:
        n = min(50, count - len(out))
        params = {"q": query, "format": "json", "count": n, "start": start}
        if since:
            params["from"] = since
        if until:
            params["until"] = until
        data = http.get_json(http.build_url(BASE, params)) or {}
        items = _extract_items(data)
        if not items:
            break

        for it in items:
            p = _to_paper(it)
            if p:
                out.append(p)
        start += len(items)
        if len(items) < n:
            break   # 結果の末尾に到達
    return out[:count]
