"""直近の検索結果をローカルに保存し、番号で参照できるようにする。"""

import os
import json

from .model import Paper

STATE_DIR = os.path.expanduser("~/.local/state/sfc-search")
LAST_PATH = os.path.join(STATE_DIR, "last_search.json")


def save(query, papers, meta=None):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LAST_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "query": query,
            "meta": meta or {},
            "items": [p.to_dict() for p in papers],
        }, f, ensure_ascii=False)


def load():
    if not os.path.exists(LAST_PATH):
        raise SystemExit("直近の検索がありません。まず `sfc-search search` を実行してください。")
    with open(LAST_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("query", ""), [Paper(**d) for d in data.get("items", [])]


def resolve(targets):
    """番号（1始まり）や DOI から Paper を引く。"""
    _, items = load()
    out = []
    for t in targets:
        t = str(t).strip()
        if t.isdigit():
            i = int(t) - 1
            if 0 <= i < len(items):
                out.append(items[i])
                continue
            raise SystemExit(f"番号 {t} は範囲外です（1〜{len(items)}）")
        hit = next((p for p in items
                    if p.doi == t.lower() or t in p.ids.values()), None)
        if hit is None:
            raise SystemExit(f"見つかりません: {t}")
        out.append(hit)
    return out


def update(index, **fields):
    """直近検索の index 番（1始まり）を更新して保存し直す。"""
    query, items = load()
    i = index - 1
    if not (0 <= i < len(items)):
        raise SystemExit(f"番号 {index} は範囲外です（1〜{len(items)}）")
    for k, v in fields.items():
        setattr(items[i], k, v)
    save(query, items)
    return items[i]
