"""
HTTP レイヤ。このツールの「クリーンさ」はほぼ全部ここに集約されている。

方針:
  1. User-Agent で正直に名乗る（ツール名 + リポジトリURL + 連絡先）。
     ブラウザを騙る偽装はしない。問題があれば遮断ではなく連絡が来るようにする。
  2. 逐次実行のみ。並列取得は一切しない。
  3. ホストごとに最低リクエスト間隔を強制する。呼び出し側からは短縮できない。
  4. 403 が返ったら「そこに来るなと言われた」ということ。UA を偽装して回避せず、
     即座に中断してユーザにブラウザ利用を案内する。
  5. 429/503 は Retry-After を尊重して指数バックオフ。3回で諦める。
"""

import os
import sys
import time
import json
import urllib.error
import urllib.parse
import urllib.request

from . import config

__all__ = ["get", "get_json", "get_binary", "PolitelyRefused", "RateLimited"]

REPO_URL = "https://github.com/wannasleep05-kekekei/sfc-survey"

# ホストごとの最低リクエスト間隔（秒）。迷ったら長い方に倒す。
_INTERVALS = {
    "api.openalex.org":      1.0,
    "api.crossref.org":      1.0,
    "api.unpaywall.org":     1.0,
    "cir.nii.ac.jp":         2.0,
    "ndlsearch.ndl.go.jp":   2.0,   # NDL は「多重アクセスを避けよ」とのみ。長めに取る
}
_DEFAULT_INTERVAL = 2.0

_MAX_RETRIES = 3
_TIMEOUT = 30

# ホストごとの最終リクエスト時刻。
# プロセスをまたいで守るためディスクに永続化する。エージェント（Claude Code 等）が
# コマンドを連続起動すると、プロセス内変数だけでは各回の1発目が待たずに飛んでしまう。
_STATE_DIR = os.path.expanduser("~/.local/state/sfc-search")
_STATE_PATH = os.path.join(_STATE_DIR, "last_request.json")

_last_request = {}


def _load_state():
    global _last_request
    if _last_request:
        return _last_request
    try:
        with open(_STATE_PATH, encoding="utf-8") as f:
            _last_request = {k: float(v) for k, v in json.load(f).items()}
    except Exception:
        _last_request = {}
    return _last_request


def _save_state():
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        with open(_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(_last_request, f)
    except Exception:
        pass      # 記録できなくても本処理は続行する


def _mark(host):
    _last_request[host] = time.time()
    _save_state()


class PolitelyRefused(Exception):
    """403 等。プログラムアクセスが許可されていないと判断される応答。"""


class RateLimited(Exception):
    """429/503 が続いた。時間をおくべき状態。"""


def user_agent():
    """正直な User-Agent を組み立てる。連絡先は config から。"""
    contact = config.contact()
    if contact:
        return f"sfc-search-tools/0.1 (+{REPO_URL}; contact: {contact})"
    return f"sfc-search-tools/0.1 (+{REPO_URL})"


def _throttle(host):
    interval = _INTERVALS.get(host, _DEFAULT_INTERVAL)
    state = _load_state()
    elapsed = time.time() - state.get(host, 0.0)
    if 0 <= elapsed < interval:
        time.sleep(interval - elapsed)


def _retry_after(err, attempt):
    raw = err.headers.get("Retry-After") if err.headers else None
    if raw and raw.strip().isdigit():
        return min(int(raw.strip()), 120)
    return min(10 * (2 ** attempt), 120)


def _request(url, *, binary=False, accept=None, max_bytes=None, quiet=False):
    host = urllib.parse.urlparse(url).netloc
    headers = {
        "User-Agent": user_agent(),
        "Accept-Language": "ja,en;q=0.8",
    }
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)

    for attempt in range(_MAX_RETRIES):
        _throttle(host)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
                _mark(host)
                data = r.read(max_bytes) if max_bytes else r.read()
                final_url = r.geturl()
                if binary:
                    return data, final_url
                return data.decode("utf-8", "replace"), final_url

        except urllib.error.HTTPError as e:
            _mark(host)

            if e.code in (401, 403):
                raise PolitelyRefused(
                    f"{host} が {e.code} を返しました。\n"
                    f"  このエンドポイントへのプログラムアクセスは許可されていません。\n"
                    f"  User-Agent を偽装して回避することはしません。\n"
                    f"  このソースを --source から外して再実行してください。"
                ) from e

            if e.code == 404:
                return (b"" if binary else ""), url

            if e.code in (429, 500, 502, 503, 504):
                if attempt == _MAX_RETRIES - 1:
                    raise RateLimited(
                        f"{host} が {e.code} を返し続けています。時間をおいて再実行してください。"
                    ) from e
                back = _retry_after(e, attempt)
                if not quiet:
                    print(f"[sfc-search] {host} {e.code}: {back}秒待機します",
                          file=sys.stderr)
                time.sleep(back)
                continue

            raise

        except urllib.error.URLError as e:
            if attempt == _MAX_RETRIES - 1:
                raise
            time.sleep(3 * (attempt + 1))

    raise RateLimited(f"{host}: リトライ上限に達しました")


def get(url, **kw):
    """テキストを取得して (body, final_url) を返す。"""
    return _request(url, **kw)


def get_json(url, **kw):
    """JSON を取得して dict/list を返す。空応答なら None。"""
    body, _ = _request(url, accept="application/json", **kw)
    if not body.strip():
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def get_binary(url, max_bytes=None, **kw):
    """バイナリを取得して (bytes, final_url) を返す。"""
    return _request(url, binary=True, max_bytes=max_bytes, **kw)


def build_url(base, params):
    """None 値を落として URL を組み立てる。"""
    clean = {k: v for k, v in params.items() if v not in (None, "", [])}
    return base + "?" + urllib.parse.urlencode(clean)
