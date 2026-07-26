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


def _print_list(papers, verbose=False):
    for i, p in enumerate(papers, 1):
        print(f"\n[{i}] {_tag(p)} {p.title}")
        line = f"    {p.author_str()}"
        if p.venue:
            line += f" / {p.venue}"
        if p.year:
            line += f" ({p.year})"
        print(line)
        bits = []
        if p.cited_by:
            bits.append(f"被引用 {p.cited_by}")
        if p.doi:
            bits.append(f"doi:{p.doi}")
        if p.sources:
            bits.append("+".join(p.sources))
        if bits:
            print("    " + "  ".join(bits))
        if p.holding:
            h = p.holding
            print(f"    所蔵: {h.get('library','')} {h.get('location','')} "
                  f"請求記号 {h.get('callnumber','')} [{h.get('status','')}]")
        if verbose and p.abstract:
            print(f"    {p.abstract[:300]}…")


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def cmd_search(args):
    names = args.source or DEFAULT_SOURCES
    if args.books:
        names = [n for n in names if n in ("ndl", "cinii")] or ["ndl"]

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
            is_book = (p.type or "").lower() in ("book", "book-chapter", "図書")
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

    print(f"# 慶應所蔵チェックリスト（未確認 {len(unchecked)}/{len(targets)} 件）")
    print("#")
    print("# KOSMOS は URL で渡した検索語をそのまま実行してくれません（実測で確認）。")
    print("# 下のリンクは【検索欄に語句が入った状態】で KOSMOS を開きます。")
    print("# 開いたら虫眼鏡ボタンを押して検索し直してください。")
    print("# 自動で表示される結果は当てになりません。")
    print("#")
    print("# 結果は `sfc-search hold <番号> --library ... --call ...` で記録します。\n")

    # 同じ雑誌の論文が複数あれば1つにまとめる（同じ検索を何度も開かせない）
    groups = {}
    for i, p, u in ok:
        groups.setdefault(u, []).append((i, p))

    for u, members in groups.items():
        i0, p0 = members[0]
        is_book = (p0.type or "").lower() in ("book", "book-chapter", "図書")
        nums = ", ".join(f"[{i}]" for i, _ in members)
        if is_book:
            print(f"{nums} {p0.title}")
            print("    書名で照会")
        else:
            print(f"{nums} 掲載誌「{p0.venue[:30]}」")
            for _, p in members:
                print(f"      - {p.title[:56]}")
        print(f"    検索語: {kosmos.clean_query(p0.title if is_book else p0.venue)}")
        print(f"    {u}\n")

    if ng:
        print(f"--- 照会できないもの（{len(ng)} 件）---")
        print("掲載誌が不明、または実在の雑誌名でないレコードです。")
        print("原文にあたるか、DOI・タイトルで直接検索してください。\n")
        for i, p in ng:
            why = f"誌名「{p.venue[:30]}」は雑誌名ではない" if p.venue else "誌名なし"
            print(f"[{i}] {p.title[:52]}")
            print(f"    {why}\n")

    if ok:
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
                    "doi", "cited_by", "oa", "holding"])
        for i, p in enumerate(items, 1):
            w.writerow([i, p.title, "; ".join(p.authors), p.year, p.venue,
                        p.doi, p.cited_by, "yes" if (p.is_oa or p.oa_url) else "",
                        p.holding.get("callnumber", "") if p.holding else ""])
        text = buf.getvalue()
    else:  # markdown
        lines = [f"# 検索: {query}", "",
                 f"{len(items)} 件", ""]
        for i, p in enumerate(items, 1):
            lines.append(f"## [{i}] {p.title}")
            lines.append(f"- 著者: {', '.join(p.authors) or '不明'}")
            lines.append(f"- 出典: {p.venue or '不明'} ({p.year or '年不明'})")
            if p.doi:
                lines.append(f"- DOI: {p.doi}")
            if p.cited_by:
                lines.append(f"- 被引用数: {p.cited_by}")
            if p.holding:
                h = p.holding
                lines.append(f"- 慶應所蔵: {h.get('library','')} "
                             f"請求記号 {h.get('callnumber','')}")
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
    _, items = store.load()
    missing = [p for p in items
               if not p.holding and not (p.is_oa or p.oa_url)]
    if args.limit:
        missing = missing[:args.limit]
    print(f"# 文献複写依頼リスト（{len(missing)} 件）")
    print("# メディアセンターの ILL 窓口に提出できる形式です\n")
    for i, p in enumerate(missing, 1):
        print(f"{i}. {p.title}")
        print(f"   著者: {', '.join(p.authors) or '不明'}")
        print(f"   誌名: {p.venue or '不明'}")
        print(f"   年  : {p.year or '不明'}")
        if p.doi:
            print(f"   DOI : {p.doi}")
        print()


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

    p = sub.add_parser("ill", help="所蔵なし・非OAを文献複写依頼形式で出力")
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=cmd_ill)

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
