"""
Crossref (https://www.crossref.org)

DOI 登録機関の公式 API。OpenAlex が拾いきれない新しめの論文や、書籍章の書誌が強い。
mailto を付けると polite pool に入る。
"""

from .. import http, config
from ..model import Paper, norm_doi, norm_year

BASE = "https://api.crossref.org/works"
NAME = "crossref"

_TYPE_MAP = {
    "journal-article": "article",
    "book": "book",
    "book-chapter": "book-chapter",
    "proceedings-article": "proceedings-article",
    "dissertation": "dissertation",
}


def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def _to_paper(it):
    if not it:
        return None
    authors = []
    for a in (it.get("author") or []):
        name = a.get("name") or " ".join(
            x for x in (a.get("given"), a.get("family")) if x)
        if name:
            authors.append(name.strip())

    year = ""
    for k in ("published-print", "published-online", "issued", "created"):
        parts = ((it.get(k) or {}).get("date-parts") or [[]])[0]
        if parts:
            year = norm_year(parts[0])
            break

    raw_type = it.get("type") or ""
    isbns = it.get("ISBN") or []
    return Paper(
        title=_first(it.get("title")).strip(),
        authors=authors,
        year=year,
        venue=_first(it.get("container-title")).strip() or (it.get("publisher") or ""),
        type=_TYPE_MAP.get(raw_type, raw_type),
        doi=norm_doi(it.get("DOI")),
        abstract=_strip_jats(it.get("abstract") or ""),
        cited_by=int(it.get("is-referenced-by-count") or 0),
        landing_url=it.get("URL") or "",
        volume=str(it.get("volume") or ""),
        issue=str(it.get("issue") or ""),
        pages=str(it.get("page") or ""),
        isbn=(_first(isbns) or "").replace("-", ""),
        sources=[NAME],
        ids={"crossref": it.get("DOI") or ""},
    )


def _strip_jats(s):
    import re
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def search(query, count=50, since=None, until=None):
    out, offset = [], 0
    while len(out) < count:
        rows = min(100, count - len(out))
        filters = []
        if since:
            filters.append(f"from-pub-date:{since}-01-01")
        if until:
            filters.append(f"until-pub-date:{until}-12-31")
        url = http.build_url(BASE, {
            # query= は語をばらして緩くマッチするため、日本語だと「障害者」だけ拾って
            # 「参政権」を落とす。query.bibliographic は書誌全体に対する厳密寄りの照合。
            "query.bibliographic": query,
            "rows": rows,
            "offset": offset,
            "filter": ",".join(filters) or None,
            "mailto": config.require_contact(),
        })
        data = http.get_json(url) or {}
        items = ((data.get("message") or {}).get("items") or [])
        if not items:
            break
        for it in items:
            p = _to_paper(it)
            if p and p.title:
                out.append(p)
        offset += len(items)
        if len(items) < rows:
            break   # 結果の末尾に到達
    return out[:count]


def by_doi(doi):
    key = norm_doi(doi)
    if not key:
        return None
    data = http.get_json(http.build_url(f"{BASE}/{key}",
                                        {"mailto": config.require_contact()}))
    if not data:
        return None
    return _to_paper((data or {}).get("message"))


def _raw_by_doi(doi):
    data = http.get_json(http.build_url(f"{BASE}/{norm_doi(doi)}",
                                        {"mailto": config.require_contact()}))
    return (data or {}).get("message") or {}


def chapters(isbn=None, doi=None):
    """
    書籍の章立て（＝実質的な目次）を返す。[(章題, DOI, ページ範囲), ...]

    Crossref に章ごとの DOI を登録している出版社なら、ISBN で引くと章が全部返る。
    実測（2026-07-27）: Klein, Selling Out (Bloomsbury, 2020) は
    ISBN 9781501339349 で本体1件＋章9件、いずれもページ範囲付きで取得できた。

    日本の出版社は章 DOI をほぼ登録していないため、和書ではまず空になる。
    その場合は目次を推測せず「未確認」として扱うこと（呼び出し側の責任）。
    """
    if not isbn and doi:
        rec = _raw_by_doi(doi)
        for v in (rec.get("ISBN") or []):
            isbn = (v or "").replace("-", "")
            if isbn:
                break
    isbn = (isbn or "").replace("-", "").strip()
    if not isbn:
        return []

    data = http.get_json(http.build_url(BASE, {
        "filter": f"isbn:{isbn}",
        "rows": 100,
        "select": "DOI,title,type,page",
        "mailto": config.require_contact(),
    })) or {}

    out = []
    for it in ((data.get("message") or {}).get("items") or []):
        if (it.get("type") or "") == "book":
            continue          # 本体レコードは章ではない
        title = _first(it.get("title")).strip()
        if not title:
            continue
        out.append((title, it.get("DOI") or "", str(it.get("page") or "")))

    # ページ範囲の開始位置で並べる。DOI の連番は章順と一致しないことがある。
    def _start(row):
        head = (row[2] or "").split("-")[0].strip()
        return int(head) if head.isdigit() else 10 ** 6
    out.sort(key=_start)
    return out
