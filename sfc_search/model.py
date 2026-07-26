"""
文献レコードの共通表現と名寄せ。

複数のソース（OpenAlex / Crossref / CiNii / KOSMOS）が同じ論文を返すので、
DOI を第一キー、正規化タイトル＋出版年を第二キーとして統合する。
"""

import re
import unicodedata
from dataclasses import dataclass, field, asdict


@dataclass
class Paper:
    title: str = ""
    authors: list = field(default_factory=list)
    year: str = ""
    venue: str = ""              # 掲載誌 / 出版社
    type: str = ""               # article / book / chapter ...
    doi: str = ""
    abstract: str = ""
    cited_by: int = 0
    is_oa: bool = False
    oa_url: str = ""             # OA 本文の直リンク（あれば）
    landing_url: str = ""        # 出版社ページ / 書誌ページ
    openurl: str = ""            # KOSMOS の本文解決リンク
    holding: dict = field(default_factory=dict)   # 慶應所蔵（図書館/請求記号）
    sources: list = field(default_factory=list)   # 由来（openalex, cinii, ...）
    ids: dict = field(default_factory=dict)       # 各ソースの原ID

    def key(self):
        if self.doi:
            return ("doi", self.doi)
        return ("title", norm_title(self.title), self.year or "")

    def to_dict(self):
        return asdict(self)

    def author_str(self, limit=3):
        if not self.authors:
            return ""
        head = ", ".join(self.authors[:limit])
        return head + ("…" if len(self.authors) > limit else "")


def norm_doi(raw):
    """DOI を 10.xxxx/yyy の小文字形に正規化。"""
    if not raw:
        return ""
    s = str(raw).strip()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s, flags=re.I)
    s = re.sub(r"^doi:\s*", "", s, flags=re.I)
    s = s.strip().rstrip(".")
    return s.lower() if s.startswith("10.") else ""


def norm_title(raw):
    """タイトルの表記ゆれを潰す。全角半角・記号・空白・大小文字。"""
    if not raw:
        return ""
    s = unicodedata.normalize("NFKC", str(raw)).lower()
    s = re.sub(r"[^\w\u3040-\u30ff\u4e00-\u9fff]+", "", s)
    return s


def norm_year(raw):
    if not raw:
        return ""
    m = re.search(r"(1[5-9]\d{2}|20\d{2}|21\d{2})", str(raw))
    return m.group(1) if m else ""


def clean_author(raw):
    """CiNii の "井上, 英夫 (1947-)$$Q..." のような装飾を落とす。"""
    if not raw:
        return ""
    s = str(raw).split("$$")[0]
    # "井上, 英夫 (1947-)" と "栗原, 涼子, 1953-" の両形式から生没年を落とす
    s = re.sub(r"\s*[(（]\s*\d{4}\s*-\s*\d{0,4}\s*[)）]\s*$", "", s)
    s = re.sub(r"\s*,\s*\d{4}\s*-\s*\d{0,4}\s*$", "", s)
    return s.strip(" ,")


def _better(a, b):
    """2つの値のうち情報量が多い方を選ぶ。"""
    if not a:
        return b
    if not b:
        return a
    if isinstance(a, str) and isinstance(b, str):
        return a if len(a) >= len(b) else b
    return a


def merge(base, other):
    """other の情報を base に取り込む。base を破壊的に更新して返す。"""
    base.title = _better(base.title, other.title)
    base.venue = _better(base.venue, other.venue)
    base.type = base.type or other.type
    base.year = base.year or other.year
    base.doi = base.doi or other.doi
    base.abstract = _better(base.abstract, other.abstract)
    base.cited_by = max(base.cited_by, other.cited_by)
    base.landing_url = base.landing_url or other.landing_url
    base.openurl = base.openurl or other.openurl

    if other.is_oa and not base.is_oa:
        base.is_oa = True
    base.oa_url = base.oa_url or other.oa_url

    if len(other.authors) > len(base.authors):
        base.authors = other.authors
    if other.holding and not base.holding:
        base.holding = other.holding

    for s in other.sources:
        if s not in base.sources:
            base.sources.append(s)
    for k, v in other.ids.items():
        base.ids.setdefault(k, v)
    return base


def dedupe(papers):
    """DOI → 正規化タイトルの順で名寄せ。入力順を保つ。"""
    by_doi = {}
    by_title = {}    # (正規化タイトル, 年) -> Paper
    out = []

    def _title_hit(tkey):
        """年が片方だけ空のレコードも同一とみなす（NDL は年欠落が多い）。"""
        t, y = tkey
        if not t:
            return None
        if tkey in by_title:
            return by_title[tkey]
        for (t2, y2), p in by_title.items():
            if t2 == t and (not y or not y2 or y == y2):
                return p
        return None

    for p in papers:
        hit = None
        if p.doi and p.doi in by_doi:
            hit = by_doi[p.doi]
        else:
            hit = _title_hit((norm_title(p.title), p.year or ""))

        if hit is not None:
            merge(hit, p)
            if hit.doi:
                by_doi.setdefault(hit.doi, hit)
            continue

        out.append(p)
        if p.doi:
            by_doi[p.doi] = p
        tkey = (norm_title(p.title), p.year or "")
        if tkey[0]:
            by_title[tkey] = p

    return out


# 書籍の構成要素など、文献本体でないレコードのタイトル
_NON_ARTICLE = ("目次", "まえがき", "あとがき", "奥付", "索引", "表紙", "扉",
                "はしがき", "凡例", "執筆者一覧", "Contents", "Index",
                "Front Matter", "Back Matter", "Table of Contents")


def is_non_article(paper):
    """「目次」「まえがき」等、単体では文献として意味のないレコードか。"""
    t = (paper.title or "").strip()
    if not t:
        return True
    if len(t) <= 12 and any(t.startswith(k) or t == k for k in _NON_ARTICLE):
        return True
    return False


def relevance(paper, query):
    """
    検索語との一致度を粗く採点する。
    ソースごとに関連度の基準がばらばらなので、統合後に共通の物差しで並べ直す。

    タイトル一致を最重視し、掲載誌・抄録は補助。被引用数はごく弱いタイブレークに使う
    （強くすると分野違いの巨大論文が上位に来る）。
    """
    terms = [t for t in re.split(r"\s+", (query or "").strip()) if t]
    if not terms:
        return 0.0
    title = (paper.title or "")
    venue = (paper.venue or "")
    abst = (paper.abstract or "")[:400]

    score = 0.0
    for t in terms:
        if t in title:
            score += 3.0
        elif t.lower() in title.lower():
            score += 2.5
        if t in venue:
            score += 0.5
        if t in abst:
            score += 0.5

    # 全語がタイトルに揃っていれば大きく加点
    if all(t in title for t in terms):
        score += 3.0

    import math
    score += min(math.log10(paper.cited_by + 1), 2.0) * 0.3
    if is_non_article(paper):
        score -= 5.0
    return score
