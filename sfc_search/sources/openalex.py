"""
OpenAlex (https://openalex.org)

Google Scholar の置き換え。CC0 の公開 API で、規約上まったく問題がない。
Scholar と違って引用グラフが構造化データで取れるので、先行研究サーベイの王道である
「重要な1本から引用の前後に辿る」がそのまま実装できる。
"""

from .. import http, config
from ..model import Paper, norm_doi, norm_year

BASE = "https://api.openalex.org"
NAME = "openalex"


def _abstract(inverted):
    """OpenAlex の abstract_inverted_index を平文に戻す。"""
    if not inverted:
        return ""
    positions = []
    for word, idxs in inverted.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort(key=lambda x: x[0])
    return " ".join(w for _, w in positions)


def _to_paper(w):
    if not w:
        return None
    oa = w.get("open_access") or {}
    loc = w.get("primary_location") or {}
    src = (loc.get("source") or {}) if isinstance(loc, dict) else {}

    authors = []
    for a in (w.get("authorships") or []):
        name = ((a.get("author") or {}).get("display_name") or "").strip()
        if name:
            authors.append(name)

    oa_url = oa.get("oa_url") or ""
    if not oa_url:
        for l in (w.get("locations") or []):
            if l.get("is_oa") and l.get("pdf_url"):
                oa_url = l["pdf_url"]
                break

    openalex_id = (w.get("id") or "").rsplit("/", 1)[-1]

    return Paper(
        title=(w.get("display_name") or w.get("title") or "").strip(),
        authors=authors,
        year=norm_year(w.get("publication_year")),
        venue=(src.get("display_name") or "").strip(),
        type=w.get("type") or "",
        doi=norm_doi(w.get("doi")),
        abstract=_abstract(w.get("abstract_inverted_index")),
        cited_by=int(w.get("cited_by_count") or 0),
        is_oa=bool(oa.get("is_oa")),
        oa_url=oa_url,
        landing_url=loc.get("landing_page_url") or "",
        sources=[NAME],
        ids={"openalex": openalex_id} if openalex_id else {},
    )


def _page(params, per_page, cursor=None):
    p = dict(params)
    p["per-page"] = min(per_page, 200)
    p["mailto"] = config.require_contact()
    if cursor:
        p["cursor"] = cursor
    url = http.build_url(BASE + "/works", p)
    return http.get_json(url) or {}


def _collect(params, want):
    """cursor ページングで want 件まで集める。"""
    out, cursor = [], "*"
    while len(out) < want:
        data = _page(params, want - len(out), cursor)
        results = data.get("results") or []
        if not results:
            break
        for w in results:
            p = _to_paper(w)
            if p and p.title:
                out.append(p)
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
    return out[:want]


def search(query, count=50, since=None, until=None, work_type=None):
    filters = []
    if since:
        filters.append(f"from_publication_date:{since}-01-01")
    if until:
        filters.append(f"to_publication_date:{until}-12-31")
    if work_type:
        filters.append(f"type:{work_type}")
    params = {"search": query, "filter": ",".join(filters) or None}
    return _collect(params, count)


def cited_by(doi_or_id, count=100):
    """この文献を引用している文献（前向き）。"""
    wid = _resolve_id(doi_or_id)
    if not wid:
        return []
    return _collect({"filter": f"cites:{wid}", "sort": "cited_by_count:desc"}, count)


def references(doi_or_id, count=200):
    """この文献が引用している文献（後ろ向き）。"""
    w = _fetch_work(doi_or_id)
    if not w:
        return []
    refs = (w.get("referenced_works") or [])[:count]
    if not refs:
        return []
    out = []
    # OpenAlex は openalex_id の OR フィルタを 50 件までしか受けないので分割
    for i in range(0, len(refs), 50):
        chunk = refs[i:i + 50]
        ids = "|".join(r.rsplit("/", 1)[-1] for r in chunk)
        out.extend(_collect({"filter": f"openalex_id:{ids}"}, len(chunk)))
    return out


def _fetch_work(doi_or_id):
    key = norm_doi(doi_or_id)
    ident = f"doi:{key}" if key else doi_or_id
    url = http.build_url(f"{BASE}/works/{ident}",
                         {"mailto": config.require_contact()})
    return http.get_json(url)


def _resolve_id(doi_or_id):
    if str(doi_or_id).upper().startswith("W"):
        return doi_or_id
    w = _fetch_work(doi_or_id)
    if not w:
        return None
    return (w.get("id") or "").rsplit("/", 1)[-1] or None
