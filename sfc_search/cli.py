"""sfc-search: SFC 向け先行研究サーベイ CLI。"""

import os
import sys
import json
import argparse
import webbrowser

from . import config, http, store
from .model import dedupe, relevance, is_non_article
from .sources import openalex, crossref, cinii, ndl, kosmos, oa

VERSION = "0.1.0"

# Windows の既定コードページ (cp932) だと日本語や en dash で落ちるため、
# 標準出力/標準エラーを UTF-8 に付け替える。他OSでは無害。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 検索ソース。すべて公開 API で、認証も事前許諾も不要。
# KOSMOS はここに入らない（URL を組み立てるだけで、リクエストを送らないため）。
SOURCES = {
    "openalex": openalex,
    "crossref": crossref,
    "cinii": cinii,
    "ndl": ndl,
}
DEFAULT_SOURCES = ["openalex", "crossref", "cinii", "ndl"]


# ---------------------------------------------------------------------------
# 表示
# ---------------------------------------------------------------------------

def _tag(p):
    if p.holding:
        return "[慶應所蔵]"
    if p.is_oa or p.oa_url:
        return "[OA]"
    if (p.type or "").startswith("book"):
        return "[図書]"
    return "[要アクセス]"


VERIFY_LEVELS = {
    "biblio":   "書誌のみ",
    "toc":      "目次",
    "abstract": "抄録",
    "fulltext": "本文読了",
    "partial":  "本文の一部",
}


def _verify_label(p):
    v = p.verified or {}
    lv = VERIFY_LEVELS.get(v.get("level", ""), v.get("level", ""))
    note = v.get("note", "")
    if not lv:
        return ""
    return f"{lv}（{note}）" if note else lv


def _print_list(papers, verbose=False):
    for i, p in enumerate(papers, 1):
        print(f"\n[{i}] {_tag(p)} {p.title}")
        line = f"    {p.author_str()}"
        if p.venue:
            line += f" / {p.venue}"
        if p.year:
            line += f" ({p.year})"
        loc = p.locator()
        if loc:
            line += f" {loc}"
        print(line)
        bits = []
        if p.cited_by:
            bits.append(f"被引用 {p.cited_by}")
        if p.doi:
            bits.append(f"doi:{p.doi}")
        if p.isbn:
            bits.append(f"isbn:{p.isbn}")
        if p.sources:
            bits.append("+".join(p.sources))
        if bits:
            print("    " + "  ".join(bits))
        label = _verify_label(p)
        if label:
            print(f"    確認範囲: {label}")
        if p.holding:
            h = p.holding
            print(f"    所蔵: {h.get('library','')} {h.get('location','')} "
                  f"請求記号 {h.get('callnumber','')} [{h.get('status','')}]")
        if verbose and p.abstract:
            print(f"    {p.abstract[:300]}…")


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def _warn_japanese_query(query, names, explicit):
    """
    日本語クエリで海外ソースを引くと結果が汚れることを知らせる。

    実測（2026-07-27）: 「ボーカロイド 初音ミク 二次創作 文化」を Crossref に
    投げると「初中作文教学」「經學與中國古代文學」等の中国語論文が上位を埋める。
    既定ソースのままだと気づきにくいので、明示指定が無いときだけ警告する。
    """
    if explicit:
        return
    if not kosmos._CJK_RE.search(query or ""):
        return
    noisy = [n for n in names if n in ("openalex", "crossref")]
    if not noisy:
        return
    print(f"[sfc-search] 日本語クエリです。{'/'.join(noisy)} は無関係な"
          f"中国語論文を返しがちです。\n"
          f"             和文テーマなら "
          f"`--source cinii --source ndl` に絞るのが確実です。",
          file=sys.stderr)


def cmd_search(args):
    explicit = bool(args.source)
    names = args.source or DEFAULT_SOURCES
    if args.books:
        names = [n for n in names if n in ("ndl", "cinii")] or ["ndl"]
    _warn_japanese_query(args.query, names, explicit)

    collected = []
    for name in names:
        mod = SOURCES.get(name)
        if mod is None:
            print(f"[sfc-search] 不明なソース: {name}", file=sys.stderr)
            continue
        per = args.count
        try:
            if name == "ndl":
                got = mod.search(args.query, count=per,
                                 books_only=args.books, loose=args.loose)
            else:
                got = mod.search(args.query, count=per,
                                 since=args.since, until=args.until)
        except http.PolitelyRefused as e:
            print(f"\n[sfc-search] {e}\n", file=sys.stderr)
            continue
        except http.RateLimited as e:
            print(f"\n[sfc-search] {e}\n", file=sys.stderr)
            continue
        print(f"[{name}] {len(got)} 件", file=sys.stderr)
        collected.extend(got)

    papers = dedupe(collected)
    if args.since:
        papers = [p for p in papers if p.year and int(p.year) >= args.since]
    dropped = 0
    if not args.keep_all:
        before = len(papers)
        papers = [p for p in papers if not is_non_article(p)]
        dropped = before - len(papers)

    if args.sort == "cited":
        papers.sort(key=lambda p: p.cited_by, reverse=True)
    elif args.sort == "new":
        papers.sort(key=lambda p: p.year or "", reverse=True)
    else:
        # 既定。ソースごとに関連度の基準が違うので共通の物差しで並べ直す
        papers.sort(key=lambda p: relevance(p, args.query), reverse=True)

    store.save(args.query, papers, {"sources": names})

    if args.json:
        print(json.dumps([p.to_dict() for p in papers],
                         ensure_ascii=False, indent=1))
        return

    merged = len(collected) - len(papers) - dropped
    note = f"（生 {len(collected)} 件 → 重複 {merged} 件を統合"
    note += f"、目次等 {dropped} 件を除外）" if dropped else "）"
    print(f"\n「{args.query}」 {len(papers)} 件 {note}")
    _print_list(papers, verbose=args.verbose)
    print("\n次: `sfc-search export` で絞り込み / "
          "`sfc-search holdings` で慶應の所蔵を確認")


# ---------------------------------------------------------------------------
# 引用チェイニング
# ---------------------------------------------------------------------------

def _sort_chain(papers, how):
    if how == "new":
        papers.sort(key=lambda p: p.year or "", reverse=True)
    elif how == "old":
        papers.sort(key=lambda p: p.year or "9999")
    else:
        papers.sort(key=lambda p: p.cited_by, reverse=True)
    return papers


def cmd_cited_by(args):
    papers = _sort_chain(dedupe(openalex.cited_by(args.doi, count=args.count)),
                         args.sort)
    store.save(f"cited-by:{args.doi}", papers)
    print(f"{args.doi} を引用している文献 {len(papers)} 件")
    _print_list(papers)


def cmd_refs(args):
    papers = _sort_chain(dedupe(openalex.references(args.doi, count=args.count)),
                         args.sort)
    store.save(f"refs:{args.doi}", papers)
    print(f"{args.doi} が引用している文献 {len(papers)} 件")
    _print_list(papers)


def cmd_snowball(args):
    """起点から前後1段を辿って統合する。深追いはしない。"""
    seen = []
    seen.extend(openalex.references(args.doi, count=args.count))
    seen.extend(openalex.cited_by(args.doi, count=args.count))
    papers = _sort_chain(dedupe(seen), args.sort)
    store.save(f"snowball:{args.doi}", papers)
    label = {"new": "新しい順", "old": "古い順"}.get(args.sort, "被引用数順")
    print(f"{args.doi} の前後1段: {len(papers)} 件（{label}）")
    _print_list(papers)


# ---------------------------------------------------------------------------
# 所蔵確認 / ブラウザ
# ---------------------------------------------------------------------------

def _parse_range(spec, total):
    """'1-10' '3' '2,5,7-9' を 0始まりのindexリストに。"""
    out = []
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            for i in range(int(a), int(b) + 1):
                if 1 <= i <= total:
                    out.append(i - 1)
        elif part.isdigit():
            i = int(part)
            if 1 <= i <= total:
                out.append(i - 1)
    return sorted(set(out))


def cmd_holdings(args):
    """
    慶應の所蔵を確認するための KOSMOS 検索 URL を出力する。

    このコマンドは慶應のサーバにリクエストを送らない。URL を組み立てるだけで、
    実際のアクセスはユーザがブラウザで行う。結果は `sfc-search hold` で記録する。
    """
    _, items = store.load()
    targets = items[:args.limit]

    if args.terms:
        seen = set()
        for p in targets:
            is_book = p.is_book()
            src = p.title if is_book else p.venue
            if not is_book and not kosmos.is_real_venue(p.venue):
                continue
            q = kosmos.clean_query(src)
            if q and q not in seen:
                seen.add(q)
                print(q)
        return

    if args.open:
        idxs = _parse_range(args.open, len(items))
        if not idxs:
            raise SystemExit("開く対象がありません（例: --open 1-10）")
        print(f"{len(idxs)} 件を KOSMOS で開きます", file=sys.stderr)
        opened = 0
        for i in idxs:
            p = items[i]
            u = kosmos.url_for(p)
            if not u:
                print(f"[{i+1}] skip（照会不能）{p.title[:44]}")
                continue
            print(f"[{i+1}] {p.title[:50]}")
            webbrowser.open(u)
            opened += 1
        if not opened:
            print("開けるものがありませんでした。")
            return
        print("\n確認できたら記録してください:")
        print("  sfc-search hold 3 --library SFC --call 369.27@Y16")
        print("  sfc-search hold 4 --none")
        return

    unchecked = [(i, p) for i, p in enumerate(targets, 1) if not p.holding]
    resolvable = [(i, p, kosmos.url_for(p)) for i, p in unchecked]
    ok = [(i, p, u) for i, p, u in resolvable if u]
    ng = [(i, p) for i, p, u in resolvable if not u]

    oa_ready = [(i, p) for i, p in unchecked if p.is_oa or p.oa_url]

    print(f"# 慶應所蔵チェックリスト（未確認 {len(unchecked)}/{len(targets)} 件）")
    print("#")
    print("# KOSMOS は URL で渡した検索語をそのまま実行してくれません（実測で確認）。")
    print("# 下のリンクは【検索欄に語句が入った状態】で KOSMOS を開きます。")
    print("# 開いたら虫眼鏡ボタンを押して検索し直してください。")
    print("# 自動で表示される結果は当てになりません。")
    print("#")
    print("# 雑誌は誌名がヒットしても読めるとは限りません。レコードを開いて")
    print("# 「オンラインで見る」の提供元ごとの収録範囲（開始年・直近N年の禁止）を、")
    print("# 下に併記した巻号と必ず突き合わせてください。")
    print("#")
    print("# 結果は `sfc-search hold <番号> --library ... --call ...` で記録します。\n")

    if oa_ready:
        print(f"--- 所蔵確認が不要なもの（OA本文あり: {len(oa_ready)} 件）---")
        for i, p in oa_ready:
            print(f"[{i}] {p.title[:56]}")
            print(f"    {p.oa_url or p.repo_url}")
        print()

    # 同じ雑誌の論文が複数あれば1つにまとめる（同じ検索を何度も開かせない）
    groups = {}
    for i, p, u in ok:
        if p.is_oa or p.oa_url:
            continue
        groups.setdefault(u, []).append((i, p))

    for u, members in groups.items():
        _, p0 = members[0]
        is_book = p0.is_book()
        nums = ", ".join(f"[{i}]" for i, _ in members)
        if is_book:
            print(f"{nums} {p0.title}")
            print("    書名で照会")
        else:
            print(f"{nums} 掲載誌「{p0.venue[:40]}」")
            for _, p in members:
                loc = p.locator()
                print(f"      - {p.title[:56]}")
                if loc:
                    print(f"        → 必要な巻号: {loc}"
                          + (f" ({p.year})" if p.year else ""))
        print(f"    検索語: {kosmos.clean_query(p0.title if is_book else p0.venue)}")
        print(f"    {u}")
        # 慶應に無かった場合の次の一手を、その場に置いておく。
        # 「未所蔵」で調査を止めないための導線。
        if is_book:
            cb = kosmos.cinii_books_url(p0.ncid)
            if cb:
                print(f"    慶應に無ければ国内所蔵: {cb}")
            elif p0.isbn:
                print(f"    慶應に無ければ CiNii Books を ISBN {p0.isbn} で照会")
        kind, form = kosmos.ill_route(p0)
        print(f"    それでも無ければ ILL {kind}: {form}\n")

    if ng:
        print(f"--- 照会できないもの（{len(ng)} 件）---")
        print("掲載誌が不明、または実在の雑誌名でないレコードです。")
        print("原文にあたるか、DOI・タイトルで直接検索してください。\n")
        for i, p in ng:
            why = f"誌名「{p.venue[:30]}」は雑誌名ではない" if p.venue else "誌名なし"
            print(f"[{i}] {p.title[:52]}")
            print(f"    {why}\n")

    if groups:
        print(f"まとめて開く: sfc-search holdings --open 1-{min(10, len(targets))}")
        print("検索語だけ一覧で欲しい場合: sfc-search holdings --terms")


def cmd_hold(args):
    """所蔵確認の結果を手で記録する。ill の精度がこれで決まる。"""
    if args.none:
        p = store.update(args.index, holding={})
        print(f"[{args.index}] 所蔵なしとして記録: {p.title[:50]}")
        return
    if not (args.library or args.call):
        raise SystemExit("--library / --call か、--none を指定してください")
    holding = {
        "library": args.library or "",
        "location": args.location or "",
        "callnumber": args.call or "",
        "status": args.status or "confirmed",
    }
    p = store.update(args.index, holding=holding)
    print(f"[{args.index}] 記録: {p.title[:50]}")
    print(f"    {holding['library']} {holding['location']} "
          f"請求記号 {holding['callnumber']}")


def cmd_kosmos_url(args):
    u = kosmos.url(args.query, rtype=args.type, scope=args.scope)
    print(u)
    if args.open:
        webbrowser.open(u)


def cmd_open(args):
    papers = store.resolve(args.target)
    for p in papers:
        u = p.oa_url or p.openurl or p.landing_url
        if not u and p.doi:
            u = f"https://doi.org/{p.doi}"
        if not u:
            print(f"[skip] URL 不明: {p.title}")
            continue
        print(f"開く: {p.title}\n      {u}")
        webbrowser.open(u)


# ---------------------------------------------------------------------------
# OA 取得 / エクスポート / ILL
# ---------------------------------------------------------------------------

def cmd_fetch(args):
    _, items = store.load()
    targets = store.resolve(args.target) if args.target else items[:args.limit]
    os.makedirs(args.out, exist_ok=True)
    ok = 0
    for p in targets:
        r = oa.fetch(p, args.out)
        if r["ok"]:
            ok += 1
            print(f"OK   {p.title[:50]}  {r['bytes']//1024}KB")
        else:
            hint = r.get("openurl") or r.get("landing") or ""
            print(f"skip {p.title[:50]}  ({r['reason']}) {hint}")
    print(f"\n{ok}/{len(targets)} 件を取得（保存先: {args.out}）")
    if ok < len(targets):
        print("取れなかったものは `sfc-search open <番号>` でブラウザから、"
              "または `sfc-search ill` で文献複写依頼へ。")


def cmd_export(args):
    query, items = store.load()
    items = items[:args.limit]

    if args.format == "json":
        text = json.dumps([p.to_dict() for p in items],
                          ensure_ascii=False, indent=1)
    elif args.format == "csv":
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["no", "title", "authors", "year", "venue",
                    "volume", "issue", "pages", "doi", "isbn", "ncid",
                    "cited_by", "oa", "holding", "verified"])
        for i, p in enumerate(items, 1):
            w.writerow([i, p.title, "; ".join(p.authors), p.year, p.venue,
                        p.volume, p.issue, p.pages, p.doi, p.isbn, p.ncid,
                        p.cited_by, "yes" if (p.is_oa or p.oa_url) else "",
                        p.holding.get("callnumber", "") if p.holding else "",
                        (p.verified or {}).get("level", "")])
        text = buf.getvalue()
    else:  # markdown
        lines = [f"# 検索: {query}", "",
                 f"{len(items)} 件", ""]
        for i, p in enumerate(items, 1):
            lines.append(f"## [{i}] {p.title}")
            lines.append(f"- 著者: {', '.join(p.authors) or '不明'}")
            src = f"- 出典: {p.venue or '不明'} ({p.year or '年不明'})"
            loc = p.locator()
            if loc:
                src += f" {loc}"
            lines.append(src)
            if p.doi:
                lines.append(f"- DOI: {p.doi}")
            if p.isbn:
                lines.append(f"- ISBN: {p.isbn}")
            if p.cited_by:
                lines.append(f"- 被引用数: {p.cited_by}")
            # 入手経路。報告に必ず書く項目なので、書誌と同じ場所に出しておく。
            if p.oa_url:
                lines.append(f"- 入手: OA 本文 {p.oa_url}")
            elif p.repo_url:
                lines.append(f"- 入手: リポジトリ書誌 {p.repo_url}（本文の有無は未確認）")
            if p.holding:
                h = p.holding
                lines.append(f"- 慶應所蔵: {h.get('library','')} "
                             f"請求記号 {h.get('callnumber','')}")
            elif not (p.is_oa or p.oa_url):
                u = kosmos.url_for(p)
                if u:
                    lines.append(f"- 慶應所蔵確認: {u}")
                if p.ncid:
                    lines.append(f"- 国内所蔵: {kosmos.cinii_books_url(p.ncid)}")
                kind, form = kosmos.ill_route(p)
                lines.append(f"- 無ければ ILL {kind}: {form}")
            lines.append(f"- 確認範囲: {_verify_label(p) or '**未記録**'}")
            if p.abstract and args.with_abstract:
                lines.append(f"- 抄録: {p.abstract}")
            lines.append("")
        text = "\n".join(lines)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"書き出しました: {args.out}（{len(items)} 件）")
    else:
        print(text)


def cmd_ill(args):
    """
    ILL 依頼リストを出す。複写（論文）と貸借（図書）を分ける。

    メディアセンターの規則（2026-07-27 確認）に合わせている:
      - 雑誌は取寄せ不可。論文は「文献複写」で、1論文につき1件
      - 図書は「取寄せ（貸借）」。取り寄せた資料は館内利用のみ
      - どちらも申込前に KOSMOS で学内所蔵を確認する必要がある
        （受取キャンパスに所蔵があると申込めない）
      - 1件 3,000円までメディアセンターが補助
    """
    _, items = store.load()
    missing = [p for p in items
               if not p.holding and not (p.is_oa or p.oa_url)]
    if args.limit:
        missing = missing[:args.limit]

    books = [p for p in missing if p.is_book()]
    arts = [p for p in missing if not p.is_book()]

    print(f"# ILL 依頼リスト（{len(missing)} 件: 複写 {len(arts)} / 貸借 {len(books)}）")
    print("# 申込前に KOSMOS で学内所蔵を確認してください。")
    print("# 受取キャンパスに所蔵があると申込めません。1件3,000円まで補助あり。\n")

    def _emit(p, n, need_pages):
        print(f"{n}. {p.title}")
        print(f"   著者: {', '.join(p.authors) or '不明'}")
        print(f"   {'出版者' if p.is_book() else '誌名'}: {p.venue or '不明'}")
        print(f"   年  : {p.year or '不明'}")
        loc = p.locator()
        if loc:
            print(f"   巻号ページ: {loc}")
        elif need_pages:
            # 巻号ページが無いと窓口で差し戻される。推測で埋めないこと。
            print("   巻号ページ: **不明（要確認）** ← この状態では申込めません")
        if p.doi:
            print(f"   DOI : {p.doi}")
        if p.isbn:
            print(f"   ISBN: {p.isbn}")
        if p.ncid:
            print(f"   NCID: {p.ncid}  国内所蔵: {kosmos.cinii_books_url(p.ncid)}")
        print()

    if arts:
        print(f"## 文献複写（申込: {kosmos.ILL_COPY_FORM} ）\n")
        for i, p in enumerate(arts, 1):
            _emit(p, i, need_pages=True)

    if books:
        print(f"## 図書取寄せ・貸借（案内: {kosmos.ILL_BOOK_SECTION} ）")
        print("   ※取り寄せた図書は館内利用のみ。1冊につき1件。\n")
        for i, p in enumerate(books, 1):
            _emit(p, i, need_pages=False)


def cmd_verified(args):
    """
    その文献をどこまで確認したかを記録する。

    報告時に「書誌だけ見た」のか「本文を読んだ」のかを書き分けるための欄。
    タイトルからの推測を確認済みとして混ぜないために、明示的に手で入れる。
    """
    if args.level not in VERIFY_LEVELS:
        raise SystemExit(f"--level は {', '.join(VERIFY_LEVELS)} のいずれか")
    import datetime
    rec = {
        "level": args.level,
        "note": args.note or "",
        "date": datetime.date.today().isoformat(),
    }
    p = store.update(args.index, verified=rec)
    print(f"[{args.index}] {p.title[:50]}")
    print(f"    確認範囲: {_verify_label(p)}")


def cmd_toc(args):
    """
    書籍の章立て（目次）を Crossref から取る。

    取れなければ「取れなかった」と言う。目次を推測で書いてはいけないので、
    代わりに人が確認できる先（NDL / CiNii Books / 出版社）を出す。
    """
    isbn, doi = "", ""
    target = args.target.strip()
    if target.isdigit():
        p = store.resolve([target])[0]
        isbn, doi = p.isbn, p.doi
        print(f"対象: {p.title}")
        if p.ncid:
            print(f"  NCID {p.ncid} / 国内所蔵 {kosmos.cinii_books_url(p.ncid)}")
    elif target.lower().startswith("10."):
        doi = target
    else:
        isbn = target.replace("-", "")

    rows = crossref.chapters(isbn=isbn, doi=doi)
    if not rows:
        print("\nCrossref に章立ての登録がありません（＝目次は未確認）。")
        print("和書は章 DOI をほぼ登録していないため、ここは空になるのが普通です。")
        print("目次は次のいずれかで人が確認してください:")
        if isbn:
            print(f"  NDLサーチ   https://ndlsearch.ndl.go.jp/search?cs=bib&keyword={isbn}")
            print(f"  KOSMOS      {kosmos.url(isbn)}")
        print("  出版社の書誌ページ（「目次」欄があることが多い）")
        return

    print(f"\n章立て {len(rows)} 件（Crossref / ページ順）")
    for title, cdoi, page in rows:
        print(f"  {page or '—':>10}  {title}")
        if args.doi_too and cdoi:
            print(f"              doi:{cdoi}")
    print("\n※これは章題とページ範囲であって本文ではありません。")
    print("  報告時の確認範囲は『目次』にとどめてください。")


def cmd_config(args):
    if args.contact:
        path = config.set_contact(args.contact)
        print(f"連絡先を保存しました: {path}")
    print(f"contact    : {config.contact() or '(未設定)'}")
    print(f"User-Agent : {http.user_agent()}")


# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        prog="sfc-search",
        description="SFC 向け先行研究サーベイ CLI（認証なし・公開APIのみ）")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search", help="複数ソースを横断検索して名寄せ")
    p.add_argument("query")
    p.add_argument("--source", action="append",
                   choices=list(SOURCES), help="繰り返し指定可")
    p.add_argument("--books", action="store_true",
                   help="書籍に絞る（NDL の category=図書 で判定）")
    p.add_argument("--loose", action="store_true",
                   help="NDL を全文検索(any)にする。再現率は上がるが精度は落ちる")
    p.add_argument("--count", type=int, default=50, help="ソースごとの取得件数")
    p.add_argument("--since", type=int)
    p.add_argument("--until", type=int)
    p.add_argument("--sort", choices=("relevance", "cited", "new"),
                   default="relevance", help="既定は関連度（タイトル一致重視）")
    p.add_argument("--keep-all", action="store_true",
                   help="「目次」「まえがき」等の非文献レコードも残す")
    p.add_argument("--verbose", action="store_true", help="抄録も表示")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("cited-by", help="この文献を引用している文献")
    p.add_argument("doi")
    p.add_argument("--count", type=int, default=100)
    p.add_argument("--sort", choices=("cited", "new", "old"),
                   default="cited", help="既定は被引用数順")
    p.set_defaults(func=cmd_cited_by)

    p = sub.add_parser("refs", help="この文献が引用している文献")
    p.add_argument("doi")
    p.add_argument("--count", type=int, default=200)
    p.add_argument("--sort", choices=("cited", "new", "old"),
                   default="cited", help="既定は被引用数順")
    p.set_defaults(func=cmd_refs)

    p = sub.add_parser("snowball", help="前後1段を辿って被引用数順に並べる")
    p.add_argument("doi")
    p.add_argument("--count", type=int, default=100)
    p.add_argument("--sort", choices=("cited", "new", "old"),
                   default="cited", help="既定は被引用数順")
    p.set_defaults(func=cmd_snowball)

    p = sub.add_parser("holdings",
                       help="慶應所蔵の確認用URL一覧（リクエストは送らない）")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--open", metavar="RANGE",
                   help="指定範囲をブラウザで開く（例: 1-10 / 2,5,7-9）")
    p.add_argument("--terms", action="store_true",
                   help="KOSMOS に入力する検索語だけを一覧出力（コピペ用）")
    p.set_defaults(func=cmd_holdings)

    p = sub.add_parser("hold", help="所蔵確認の結果を記録する")
    p.add_argument("index", type=int, help="直近検索の番号")
    p.add_argument("--library", help="所蔵館（例: SFCメディアセンター）")
    p.add_argument("--location", help="配架場所（例: 開架）")
    p.add_argument("--call", help="請求記号")
    p.add_argument("--status", help="状態（既定: confirmed）")
    p.add_argument("--none", action="store_true", help="所蔵なしとして記録")
    p.set_defaults(func=cmd_hold)

    p = sub.add_parser("kosmos-url",
                       help="KOSMOS 検索URLを組み立てる（リクエストを出さない）")
    p.add_argument("query")
    p.add_argument("--type", choices=kosmos.RTYPES)
    p.add_argument("--scope", choices=("all", "catalog"), default="all")
    p.add_argument("--open", action="store_true", help="ブラウザで開く")
    p.set_defaults(func=cmd_kosmos_url)

    p = sub.add_parser("open", help="ブラウザで開く（契約物はここから正規に）")
    p.add_argument("target", nargs="+", help="番号 または DOI")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("fetch", help="OA本文のみ取得（契約物には触れない）")
    p.add_argument("target", nargs="*")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--out", default="./papers")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("export", help="絞り込み用に書き出し")
    p.add_argument("--format", choices=("md", "json", "csv"), default="md")
    p.add_argument("--with-abstract", action="store_true")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--out")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("ill", help="所蔵なし・非OAを ILL 依頼形式で出力（複写/貸借を分ける）")
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=cmd_ill)

    p = sub.add_parser("verified", help="どこまで中身を確認したかを記録する")
    p.add_argument("index", type=int, help="直近検索の番号")
    p.add_argument("--level", required=True, choices=list(VERIFY_LEVELS),
                   help="biblio=書誌のみ / toc=目次 / abstract=抄録 / "
                        "partial=本文の一部 / fulltext=本文読了")
    p.add_argument("--note", help="例: 第5章3節のみ / 出版社要旨まで")
    p.set_defaults(func=cmd_verified)

    p = sub.add_parser("toc", help="書籍の章立て（目次）を Crossref から取る")
    p.add_argument("target", help="直近検索の番号 / DOI / ISBN")
    p.add_argument("--doi-too", action="store_true", help="章ごとの DOI も出す")
    p.set_defaults(func=cmd_toc)

    p = sub.add_parser("config", help="連絡先の設定・確認")
    p.add_argument("--contact", help="User-Agent と polite pool に載せるメールアドレス")
    p.set_defaults(func=cmd_config)

    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n中断しました", file=sys.stderr)
        sys.exit(130)
    except http.PolitelyRefused as e:
        print(f"\n[sfc-search] {e}\n", file=sys.stderr)
        sys.exit(3)
    except http.RateLimited as e:
        print(f"\n[sfc-search] {e}\n", file=sys.stderr)
        sys.exit(4)


if __name__ == "__main__":
    main()
