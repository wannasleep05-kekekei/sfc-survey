"""
CiNii Research (https://cir.nii.ac.jp)

NII が公開している OpenSearch API を使う。日本語論文・紀要・学会誌のカバレッジは
OpenAlex や Crossref より明確に強く、SFC の日本語文献サーベイでは外せない。

認証は一切使わない。旧実装にあった慶應学認の自動ログインは削除済み。
"""

from .. import http
from ..model import Paper, norm_doi, norm_year, clean_author

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


def _to_paper(item):
    title = _text(item.get("dc:title") or item.get("title"))
    if not title:
        return None

    authors = []
    for c in _as_list(item.get("dc:creator")):
        name = clean_author(_text(c))
        if name:
            authors.append(name)

    venue = _text(item.get("prism:publicationName"))
    year = norm_year(_text(item.get("prism:publicationDate"))
                     or _text(item.get("dc:date")))

    landing = item.get("@id") or _text(item.get("link"))
    if isinstance(landing, dict):
        landing = landing.get("@id", "")

    doi = ""
    for ident in _as_list(item.get("dc:identifier")):
        cand = norm_doi(_text(ident) if not isinstance(ident, str) else ident)
        if cand:
            doi = cand
            break
    if not doi:
        doi = norm_doi(_text(item.get("prism:doi")))

    return Paper(
        title=title,
        authors=authors,
        year=year,
        venue=venue,
        type=_text(item.get("dc:type")) or "article",
        doi=doi,
        abstract=_text(item.get("dc:description")),
        landing_url=landing or "",
        sources=[NAME],
        ids={"cinii": landing or ""},
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
