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
from ..model import norm_doi

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


def resolve(paper):
    """Unpaywall で OA 本文 URL を解決し、paper を更新する。"""
    if paper.oa_url:
        return paper.oa_url
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
    if u and not _is_paywalled_host(u):
        paper.oa_url = u
    return paper.oa_url


def _find_pdf(page_html, base_url):
    m = (re.search(r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)',
                   page_html, re.I)
         or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url',
                      page_html, re.I))
    if m:
        return urllib.parse.urljoin(base_url, html.unescape(m.group(1)))
    m = re.search(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', page_html, re.I)
    if m:
        return urllib.parse.urljoin(base_url, html.unescape(m.group(1)))
    return None


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
