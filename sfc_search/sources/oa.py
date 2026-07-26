"""
オープンアクセス本文の解決と取得。

取りに行くのは以下だけ:
  - Unpaywall が OA と判定した本文
  - J-STAGE / 機関リポジトリ / arXiv / PMC など、誰でも読める場所
  - OpenAlex が oa_url を持っているもの

慶應が契約している有料フルテキストには**触れない**。契約物は openurl を返すだけで、
本文取得はユーザがブラウザで慶應ログインして行う。
"""

import os
import re
import html
import urllib.parse

from .. import http, config
from ..model import norm_doi, REPO_HOST_HINTS

UNPAYWALL = "https://api.unpaywall.org/v2"

# 明らかに契約フルテキストのホスト。OA解決の結果ここに来たら取得しない。
_PAYWALLED_HINTS = (
    "sciencedirect.com", "link.springer.com", "onlinelibrary.wiley.com",
    "tandfonline.com", "journals.sagepub.com", "cambridge.org/core",
    "academic.oup.com", "nature.com/articles", "jstor.org",
)


def _is_paywalled_host(u):
    low = (u or "").lower()
    return any(h in low for h in _PAYWALLED_HINTS)


def _is_repo_host(u):
    low = (u or "").lower()
    return any(h in low for h in REPO_HOST_HINTS)


def _unpaywall(paper):
    """Unpaywall に DOI で問い合わせる。OA 本文 URL か空文字を返す。"""
    if not paper.doi:
        return ""
    email = config.require_contact()
    if not email:
        return ""
    data = http.get_json(http.build_url(f"{UNPAYWALL}/{paper.doi}",
                                        {"email": email}))
    if not data:
        return ""
    if data.get("is_oa"):
        paper.is_oa = True
    best = data.get("best_oa_location") or {}
    u = best.get("url_for_pdf") or best.get("url") or ""
    return u if (u and not _is_paywalled_host(u)) else ""


def resolve(paper):
    """
    OA 本文 URL を解決し、paper を更新する。2段構えにしている。

      1. Unpaywall（DOI 必須）
      2. 機関リポジトリ / J-STAGE の書誌ページから citation_pdf_url を読む

    2 が要る理由: Unpaywall は JaLC DOI と日本の機関リポジトリをほとんど
    カバーしていない。実測（2026-07-27）では、全文 PDF が誰でも落とせる
    東京大学の博士論文（doi:10.15083/0002006211, 282頁）も、立教大学リポジトリの
    紀要論文も、Unpaywall 経由では `no_oa` と判定された。CiNii / NDL が返す
    リポジトリの書誌ページを見に行けば、どちらも citation_pdf_url で本文に届く。

    契約フルテキストのホスト（_PAYWALLED_HINTS）には 1 も 2 も踏み込まない。
    """
    if paper.oa_url:
        return paper.oa_url

    u = _unpaywall(paper)
    if u:
        paper.oa_url = u
        return u

    # リポジトリの書誌ページ候補。CiNii/NDL が拾った repo_url を優先する。
    for cand in (paper.repo_url, paper.landing_url):
        if not cand or not _is_repo_host(cand) or _is_paywalled_host(cand):
            continue
        try:
            page, final = http.get(cand)
        except Exception:
            continue
        pdf = _find_pdf(page, final)
        if pdf and not _is_paywalled_host(pdf):
            paper.oa_url = pdf
            paper.is_oa = True
            return pdf
    return ""


# 本文ではなく要旨・審査結果だけの PDF に付くファイル名の断片。
# 実測: 東大の博士論文は本文 A37476.pdf のほかに A37476_abstract.pdf と
# A37476_review.pdf を同じ書誌ページに並べており、順番だけで選ぶと取り違える。
_NOT_FULLTEXT = ("abstract", "summary", "review", "yoshi", "shinsa",
                 "要旨", "要約", "審査", "内容の要旨")


def _pdf_candidates(page_html, base_url):
    """書誌ページ内の PDF 候補 URL を出現順に返す。"""
    out = []
    for m in re.finditer(
            r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)',
            page_html, re.I):
        out.append(m.group(1))
    for m in re.finditer(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url',
            page_html, re.I):
        out.append(m.group(1))
    if not out:
        m = re.search(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', page_html, re.I)
        if m:
            out.append(m.group(1))
    seen, clean = set(), []
    for u in out:
        full = urllib.parse.urljoin(base_url, html.unescape(u))
        if full not in seen:
            seen.add(full)
            clean.append(full)
    return clean


def _find_pdf(page_html, base_url):
    """
    書誌ページから本文 PDF を1つ選ぶ。

    要旨・審査結果らしいファイル名は後回しにする。全部それらしければ
    先頭を返す（取り違えるより取らない方が良い場面は呼び出し側で判断する）。
    """
    cands = _pdf_candidates(page_html, base_url)
    if not cands:
        return None
    for u in cands:
        name = u.rsplit("/", 1)[-1].lower()
        if not any(k in name for k in _NOT_FULLTEXT):
            return u
    return cands[0]


def _safe_name(s, maxlen=90):
    s = re.sub(r"[\s/\\:*?\"<>|]+", "_", s or "").strip("_")
    return s[:maxlen] or "paper"


def fetch(paper, out_dir):
    """
    OA 本文を1件取得する。取れなければ理由を返す。取得は逐次（呼び出し側でループ）。
    戻り値: dict(ok, reason, path)
    """
    u = resolve(paper)
    if not u:
        return {"ok": False, "reason": "no_oa",
                "openurl": paper.openurl, "landing": paper.landing_url}
    if _is_paywalled_host(u):
        return {"ok": False, "reason": "paywalled", "openurl": paper.openurl}

    try:
        if u.lower().endswith(".pdf"):
            data, _ = http.get_binary(u)
        else:
            page, final = http.get(u)
            pdf = _find_pdf(page, final)
            if not pdf or _is_paywalled_host(pdf):
                return {"ok": False, "reason": "no_pdf_link", "landing": u}
            data, _ = http.get_binary(pdf)
    except Exception as e:
        return {"ok": False, "reason": f"error: {e}"}

    if data[:4] != b"%PDF":
        return {"ok": False, "reason": "not_pdf", "landing": u}

    os.makedirs(out_dir, exist_ok=True)
    stem = _safe_name(f"{paper.year}_{paper.title}")
    path = os.path.join(out_dir, stem + ".pdf")
    with open(path, "wb") as f:
        f.write(data)
    return {"ok": True, "reason": "oa", "path": path, "bytes": len(data)}
