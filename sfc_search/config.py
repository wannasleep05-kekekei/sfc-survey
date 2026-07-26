"""
設定。保持するのは連絡先メールアドレスだけ。

このツールは慶應のアカウント情報を一切保存しない。パスワードもセッション cookie も
API トークンも扱わない。設定ファイルに入るのは、User-Agent に載せる連絡先と、
OpenAlex / Crossref / Unpaywall の polite pool に渡す mailto だけ。

連絡先を入れる理由:
  - 何か問題があったとき、遮断ではなくメールが来るようにするため。
  - OpenAlex / Crossref は mailto 付きのリクエストを優先レーンで捌く（polite pool）。
    入れた方が速く、かつ相手のサーバに親切。
"""

import os
import json

CONFIG_DIR = os.path.expanduser("~/.config/sfc-search")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

_cache = None


def _load():
    global _cache
    if _cache is not None:
        return _cache
    data = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    _cache = data
    return data


def contact():
    """連絡先メールアドレス。環境変数 > 設定ファイル。未設定なら空文字。"""
    env = os.environ.get("SFC_SEARCH_CONTACT")
    if env:
        return env.strip()
    return (_load().get("contact") or "").strip()


def set_contact(email):
    global _cache
    os.makedirs(CONFIG_DIR, exist_ok=True)
    data = _load()
    data["contact"] = email.strip()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    _cache = data
    return CONFIG_PATH


def require_contact():
    """polite pool 用。未設定なら警告して None を返すが、処理は止めない。"""
    c = contact()
    if not c:
        import sys
        print("[sfc-search] 連絡先が未設定です。"
              "`sfc-search config --contact you@keio.jp` で設定してください。\n"
              "              （User-Agent に載せる & API を優先レーンで使うため）",
              file=sys.stderr)
        return None
    return c
