#!/usr/bin/env python3
"""多源网盘资源搜索（仅标准库）。默认：公开盘搜 + 海搜 + 小云搜索 + TA搜；可选盘小子、影视库。"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PANSOU_PUBLIC = "https://so.252035.xyz"
HAISOU_API = "https://haisou.cc/api/v2"
MOVIE_API = "https://meng-ge.top/api/movieData/getMoviesByType"
YUNSO_API = "https://www.yunso.net/api/opensearch.php"
PANXIAOZI_BASE = "https://pan.xiaozi.cc"
ATAW_BASE = "https://so.ataw.top"
ATAW_BIZ = ("quark", "ali", "sharepan")
ATAW_BIZ_CLOUD = {"quark": "quark", "ali": "aliyun", "sharepan": "others"}
ATAW_RESOURCE_RE = re.compile(r"/resources/(\d+)\?b=(quark|ali|sharepan)", re.I)
DEFAULT_ENGINES = frozenset({"pansou", "haisou", "yunso", "ataw"})
SKILL_VERSION = "1.7.5"

# 引擎熔断状态文件（借鉴 PanSeek「失败插件自动降级」；可用环境变量改路径）
ENGINE_STATE_PATH = os.environ.get("ENGINE_STATE_PATH") or os.path.expanduser("~/.pan_search/engine_state.json")
LINK_STATE_PATH = os.environ.get("LINK_STATE_PATH") or os.path.expanduser("~/.pan_search/link_state.json")
ENGINE_FAIL_LIMIT = 2    # 连续失败次数达到即冷却
ENGINE_COOLDOWN_MIN = 30  # 冷却时长（分钟）

# 对外一律用盘搜口径的网盘标识（canon_type 已归一，故此处只保留规范键）
CLOUD_NAMES = {
    "baidu": "百度网盘",
    "aliyun": "阿里云盘",
    "quark": "夸克网盘",
    "xunlei": "迅雷网盘",
    "uc": "UC网盘",
    "115": "115网盘",
    "mobile": "移动云盘",
    "tianyi": "天翼云盘",
    "pikpak": "PikPak",
    "guangya": "光鸭云盘",
    "123": "123网盘",
    "magnet": "磁力链接",
    "ed2k": "电驴链接",
    "lanzou": "蓝奏云",
    "others": "其他",
}

# 海搜内部用另一套平台代码；仅这两个规范标识需要转换
TO_HAISOU = {
    "aliyun": "ali",
    "mobile": "yidong",
}

# 海搜返回的 share_code 需拼成完整链接：(前缀, 提取码参数名)
HAISOU_PREFIX = {
    "ali": ("https://www.alipan.com/s/", "pwd"),
    "baidu": ("https://pan.baidu.com/s/", "pwd"),
    "quark": ("https://pan.quark.cn/s/", ""),
    "xunlei": ("https://pan.xunlei.com/s/", "pwd"),
    "tianyi": ("https://cloud.189.cn/t/", ""),
    "yidong": ("https://yun.139.com/shareweb/#/w/i/", ""),
    "115": ("https://115.com/s/", "password"),
    "123": ("https://www.123pan.com/s/", "pwd"),
    "uc": ("https://drive.uc.cn/s/", ""),
}

MOVIE_TYPE = {
    "TV": "电视剧",
    "TV_4K": "电视剧（4K）",
    "MOVIE": "电影",
    "MOVIE_4K": "电影（4K）",
    "ANIME": "动漫",
    "ANIME_4K": "动漫（4K）",
}

CANON = {
    "ali": "aliyun",
    "alipan": "aliyun",
    "yidong": "mobile",
}

YUNSO_NAMES = {
    "夸克": "quark",
    "百度": "baidu",
    "阿里": "aliyun",
    "迅雷": "xunlei",
    "UC": "uc",
    "115": "115",
    "天翼": "tianyi",
    "移动": "mobile",
    "123": "123",
    "蓝奏": "lanzou",
    "PikPak": "pikpak",
}

URL_CLOUD = (
    (r"pan\.quark\.cn", "quark"),
    (r"pan\.baidu\.com", "baidu"),
    (r"alipan\.com|aliyundrive\.com", "aliyun"),
    (r"pan\.xunlei\.com", "xunlei"),
    (r"drive\.uc\.cn|fast\.uc\.cn", "uc"),
    (r"115\.com|115cdn\.com|anxia\.com", "115"),
    (r"cloud\.189\.cn", "tianyi"),
    (r"yun\.139\.com|caiyun\.139\.com", "mobile"),
    (r"123pan\.com|123912\.com|123684\.com|123865\.com", "123"),
    (r"lanzou", "lanzou"),
    (r"mypikpak\.com", "pikpak"),
    (r"guangyapan\.com", "guangya"),
    (r"^magnet:", "magnet"),
    (r"^ed2k:", "ed2k"),
)


def canon_type(code: str) -> str:
    code = (code or "others").strip().lower()
    return CANON.get(code, code)


def cloud_from_url(url: str) -> str:
    u = url or ""
    for pat, code in URL_CLOUD:
        if re.search(pat, u, re.I):
            return code
    return "others"


def clean_share_url(url: str) -> tuple[str, str]:
    """取链接与提取码；magnet/ed2k 原样返回。"""
    u = (url or "").strip()
    if not u:
        return "", ""
    if u.lower().startswith(("magnet:", "ed2k:")):
        return u, ""
    pwd = ""
    m = re.search(r"[?&#](?:pwd|password|passcode)=([^&#]*)", u, re.I)
    if m:
        pwd = urllib.parse.unquote(m.group(1) or "")
    return u.rstrip("?&#"), pwd


def ms_to_iso(v) -> str:
    if v in (None, ""):
        return ""
    try:
        n = int(v)
        if n > 10**12:
            n //= 1000
        return datetime.fromtimestamp(n).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return str(v)[:10]


def split_csv(val: str | None) -> list[str] | None:
    if not val:
        return None
    items = [v.strip() for v in val.split(",") if v.strip()]
    return items or None


def ssl_context(insecure: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def http_json(url: str, *, method="GET", body=None, timeout=45, insecure=False, headers=None):
    """请求 JSON。SSL 先正常校验，失败再降级一次（不全程关校验）。"""
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    hdrs = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; juicePans/%s)" % SKILL_VERSION,
    }
    if data is not None:
        hdrs["Content-Type"] = "application/json; charset=utf-8"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)

    def _open(ctx):
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)

    try:
        return _open(ssl_context(insecure))
    except ssl.SSLError:
        if insecure:
            raise
        return _open(ssl_context(True))
    except TimeoutError as e:
        raise RuntimeError("请求超时") from e
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError("HTTP %s %s: %s" % (e.code, e.reason, detail)) from e
    except json.JSONDecodeError as e:
        raise RuntimeError("非 JSON 响应") from e


def http_html(url: str, timeout=30, insecure=False, retries=1):
    """抓取 HTML 页面（浏览器 UA）。SSL 先正常校验，失败再降级一次；网络类异常重试（借鉴 PanHub fetchWithRetry）。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })

    def _open(ctx):
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    last_net_err = None
    for attempt in range(retries + 1):
        try:
            return _open(ssl_context(insecure))
        except ssl.SSLError:
            if insecure:
                raise
            return _open(ssl_context(True))
        except urllib.error.HTTPError as e:
            raise RuntimeError("HTTP %s %s" % (e.code, e.reason)) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:  # 网络类异常：退避后重试
            last_net_err = e
            if attempt < retries:
                time.sleep(1 + attempt)
                continue
            raise RuntimeError("请求失败: %s" % last_net_err) from e
    raise RuntimeError("请求失败")


def ldjson_blocks(html: str):
    """提取页面内所有 application/ld+json 块（list/dict 归一化逐个产出）。"""
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            for d in data:
                yield d
        elif isinstance(data, dict):
            yield data


PAN_LINK_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")


def is_share_url(u: str) -> bool:
    """排除网盘主站入口按钮链接，只认真正的分享链接形态。"""
    if u.lower().startswith(("magnet:", "ed2k:")):
        return True
    return bool(re.search(r"/s/|/t/|/w/i/|/share", u, re.I))


def unwrap_pansou(payload: dict) -> dict:
    """盘搜响应可能是裸 merged_by_type，也可能是 {code,data}。"""
    if isinstance(payload, dict) and "code" in payload and "data" in payload:
        if payload.get("code") not in (0, None, "0"):
            raise RuntimeError(payload.get("message") or "盘搜 API 错误")
        return payload.get("data") or {}
    if isinstance(payload, dict) and "error" in payload and "merged_by_type" not in payload:
        raise RuntimeError(str(payload["error"]))
    return payload


def local_pansou_base() -> str | None:
    for cand in (
        os.environ.get("PANSOU_URL"),
        os.environ.get("NETDISK_API_URL"),
        "http://127.0.0.1:8888",
    ):
        if not cand:
            continue
        base = cand.rstrip("/")
        try:
            http_json(base + "/api/health", timeout=3)
            return base
        except Exception:
            continue
    return None


def item(cloud: str, url: str, password: str = "", note: str = "", source: str = "", dt: str = "", extra=None):
    url, pwd_in = clean_share_url(url or "")
    cloud = canon_type(cloud)
    if cloud in ("", "others"):
        cloud = cloud_from_url(url)
    rec = {
        "cloud": cloud,
        "url": url,
        "password": (password or "").strip() or pwd_in,
        "note": re.sub(r"<[^>]+>", "", note or "").strip(),
        "source": source or "",
        "datetime": dt or "",
    }
    if extra:
        rec.update(extra)
    return rec


def search_pansou(base: str, kw: str, cloud_types, include, exclude, src: str, refresh: bool, label: str, pansou_timeout: float = 45.0):
    body = {"kw": kw, "res": "merge", "src": src, "conc": 5}
    if cloud_types:
        body["cloud_types"] = [canon_type(x) for x in cloud_types]
    filt = {}
    if include:
        filt["include"] = include
    if exclude:
        filt["exclude"] = exclude
    if filt:
        body["filter"] = filt
    if refresh:
        body["refresh"] = True

    qs = {"kw": kw, "res": "merge", "src": src}
    if cloud_types:
        qs["cloud_types"] = ",".join(canon_type(x) for x in cloud_types)
    if refresh:
        qs["refresh"] = "true"
    get_url = base.rstrip("/") + "/api/search?" + urllib.parse.urlencode(qs)

    # 公开盘搜 POST 会被代理改坏/超时，默认先 GET；有服务端过滤或自建实例时先 POST
    post_first = (not base.rstrip("/").startswith("https://so.252035.xyz")) or bool(filt)
    order = (("POST", None), ("GET", get_url)) if post_first else (("GET", get_url), ("POST", None))

    last_err = None
    data = None
    for method, url in order:
        try:
            if method == "GET":
                data = unwrap_pansou(http_json(url, timeout=pansou_timeout))
            else:
                data = unwrap_pansou(
                    http_json(base.rstrip("/") + "/api/search", method="POST", body=body, timeout=pansou_timeout)
                )
            break
        except Exception as e:
            last_err = e
            continue
    if data is None:
        raise last_err or RuntimeError("盘搜失败")

    out = []
    for ctype, rows in (data.get("merged_by_type") or {}).items():
        for row in rows or []:
            out.append(
                item(
                    ctype,
                    row.get("url", ""),
                    row.get("password", ""),
                    row.get("note", ""),
                    "%s:%s" % (label, row.get("source") or "pansou"),
                    row.get("datetime", ""),
                )
            )
    return out, int(data.get("total") or len(out))


def haisou_url(platform: str, code: str, pwd: str) -> str:
    spec = HAISOU_PREFIX.get(platform)
    if not spec:
        return code
    prefix, pwd_key = spec
    url = prefix + (code or "")
    if pwd and pwd_key:
        url += "?%s=%s" % (pwd_key, urllib.parse.quote(pwd))
    return url


def search_haisou(kw: str, cloud_types, page: int, page_size: int, scope: str, min_gb, max_gb):
    # 纠错：不要把 platforms 塞成 ["all"]（会 422）；未指定网盘时整个字段省略
    body = {"query": kw, "pagination": {"page": page, "page_size": page_size}}
    filters = {}
    if scope and scope != "title":
        filters["scope"] = scope
    if cloud_types:
        mapped = []
        for x in cloud_types:
            code = TO_HAISOU.get(canon_type(x), canon_type(x))
            if code and code != "all":
                mapped.append(code)
        if mapped:
            filters["platforms"] = mapped
    if min_gb is not None:
        filters["min_size"] = int(float(min_gb) * 1024 ** 3)
    if max_gb is not None:
        filters["max_size"] = int(float(max_gb) * 1024 ** 3)
    if filters:
        body["filters"] = filters

    payload = http_json(HAISOU_API + "/shares/search", method="POST", body=body, timeout=60)
    if not payload.get("success"):
        raise RuntimeError(payload.get("message") or "海搜失败")
    data = payload.get("data") or {}
    pagination = data.get("pagination") or {}
    out = []
    for row in data.get("items") or []:
        plat = row.get("platform") or "others"
        pwd = row.get("share_pwd") or ""
        hsid = row.get("hsid") or ""
        out.append(
            item(
                plat,
                haisou_url(plat, row.get("share_code", ""), pwd),
                pwd,
                row.get("share_name", ""),
                "haisou",
                extra={
                    "files": row.get("stat_file") or 0,
                    "size": row.get("stat_size") or 0,
                    "detail": ("https://haisou.cc/j/" + hsid) if hsid else "",
                },
            )
        )
    return out, int(pagination.get("total") or len(out))


def search_yunso(kw: str, page: int, mode: str = "90001"):
    # 纠错：参数名必须是 wd（用 keyword 会触发站点人机验证返回 code<0）
    qs = urllib.parse.urlencode({"wd": kw, "mode": mode, "page": page})
    payload = http_json(YUNSO_API + "?" + qs, timeout=25)
    if payload.get("code") not in (0, None, "0"):
        raise RuntimeError(payload.get("msg") or payload.get("message") or "小云搜索失败")
    out = []
    for row in payload.get("Data") or []:
        raw_url = row.get("Scrurl") or ""
        name_hint = (row.get("Scrurlname") or "").strip()
        cloud = YUNSO_NAMES.get(name_hint) or cloud_from_url(raw_url)
        out.append(
            item(
                cloud,
                raw_url,
                row.get("Scrpass") or "",
                row.get("ScrName") or "",
                "yunso",
                ms_to_iso(row.get("addtime")),
            )
        )
    try:
        total = int(payload.get("Query_result_Total"))
    except (TypeError, ValueError):
        total = len(out)
    return out, total


def search_movie(kw: str, page: int, size: int):
    qs = urllib.parse.urlencode({"page": page, "size": size, "keyword": kw})
    payload = http_json(MOVIE_API + "?" + qs, timeout=30)
    if payload.get("code") not in (0, None, "0"):
        raise RuntimeError(payload.get("message") or "影视库失败")
    out = []
    for row in payload.get("data") or []:
        name = row.get("movieName") or ""
        typ = MOVIE_TYPE.get(row.get("type") or "", row.get("type") or "")
        note = "%s（%s）" % (name, typ) if typ else name
        dt = row.get("updateTime") or ""
        hot = {"hot": bool(row.get("hot"))}
        for cloud, link in (("baidu", row.get("baiduLink")), ("quark", row.get("quarkLink"))):
            link = (link or "").strip()
            if link:
                out.append(item(cloud, link, "", note, "movie-api", dt, extra=hot))
    return out, len(out)


def search_panxiaozi(kw: str, limit: int):
    """盘小子（pan.xiaozi.cc）：SSR 搜索页 ld+json 拿资源列表，再抓详情页取网盘直链。"""
    q = urllib.parse.urlencode({"q": kw})
    html = http_html(PANXIAOZI_BASE + "/resource?" + q, timeout=30)
    found = []
    for block in ldjson_blocks(html):
        if isinstance(block, dict) and block.get("@type") == "ItemList":
            for el in block.get("itemListElement") or []:
                url = el.get("url") or ""
                if "/resource/" in url:
                    found.append({"name": (el.get("name") or "").strip(), "url": url})
    if not found:
        return [], 0

    targets = found[: max(1, min(limit, 10))]
    detail_errors = []

    def _detail(t):
        try:
            page = http_html(t["url"], timeout=25)
        except Exception as e:
            detail_errors.append("%s: %s" % (t["url"], e))
            return []
        desc, dt, genre = "", "", ""
        for block in ldjson_blocks(page):
            if isinstance(block, dict) and block.get("@type") == "CreativeWork":
                desc = (block.get("description") or "").strip()
                dt = block.get("dateModified") or ""
                genre = (block.get("genre") or "").strip()
                break
        note = t["name"] + ("［%s］" % genre if genre else "")
        rows = []
        seen_links = set()
        for m in PAN_LINK_RE.finditer(page):
            u = m.group(0)
            if "xiaozi.cc" in u or u in seen_links or not is_share_url(u):
                continue
            seen_links.add(u)
            c = cloud_from_url(u)
            if c in ("", "others"):
                continue
            short_desc = (desc[:80] + "…") if len(desc) > 80 else desc
            note_full = (note + "：" + short_desc) if short_desc else note
            rows.append(item(c, u, "", note_full, "panxiaozi", fmt_date(dt), extra={"detail": t["url"]}))
            if len(rows) >= 6:
                break
        if not rows:
            rows = [item("others", t["url"], "", note, "panxiaozi", fmt_date(dt))]
        return rows

    out = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for rows in pool.map(_detail, targets):
            out.extend(rows)
    if not out and detail_errors:
        raise RuntimeError("详情抓取失败（" + str(len(detail_errors)) + " 个）: " + "; ".join(detail_errors[:3]))
    return out, len(found)


def ataw_cloud(biz: str, url: str) -> str:
    code = ATAW_BIZ_CLOUD.get((biz or "").strip().lower())
    if code and code != "others":
        return code
    return cloud_from_url(url)


def _ataw_search_ids(kw: str, biz: str, cap: int) -> list[tuple[str, str]]:
    qs = urllib.parse.urlencode({"q": kw, "b": biz})
    html = http_html(ATAW_BASE + "/?" + qs, timeout=30)
    seen = set()
    out = []
    for m in ATAW_RESOURCE_RE.finditer(html):
        rid, b = m.group(1), m.group(2).lower()
        key = (rid, b)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= cap:
            break
    return out


def _ataw_detail_rows(rid: str, biz_hint: str) -> list[dict]:
    data = http_json(ATAW_BASE + "/api/v1/public/resources/" + rid, timeout=25)
    if data.get("isDeleted"):
        return []
    title = (data.get("title") or "").strip()
    desc = (data.get("description") or "").strip()
    short = (desc[:80] + "…") if len(desc) > 80 else desc
    note = title + (("：" + short) if short else "")
    dt = ms_to_iso(data.get("submitTime") or data.get("updateTime") or "")
    biz = (data.get("botBiz") or biz_hint or "").lower()
    link_rows = data.get("links") or []
    if not link_rows and data.get("link"):
        link_rows = [{"platform": biz, "url": data.get("link")}]
    rows = []
    for lk in link_rows:
        if not isinstance(lk, dict):
            continue
        url = (lk.get("url") or "").strip()
        if not url or not is_share_url(url):
            continue
        plat = (lk.get("platform") or biz or "").lower()
        src = plat if plat in ATAW_BIZ_CLOUD else biz
        rows.append(item(ataw_cloud(plat, url), url, "", note, "ataw:%s" % (src or "unknown"), dt))
    return rows


def search_ataw(kw: str, limit: int = 8):
    """TA搜（so.ataw.top）：SSR 搜索页 `/?q=&b=` + 详情 `GET /api/v1/public/resources/{id}`；单 biz 失败不中断。"""
    per_biz = max(3, min(limit, 12))
    hints = []
    biz_errors = []
    for biz in ATAW_BIZ:
        try:
            hints.extend(_ataw_search_ids(kw, biz, per_biz))
        except Exception as e:
            biz_errors.append("%s: %s" % (biz, e))
    by_id = {}
    for rid, b in hints:
        by_id.setdefault(rid, b)
    targets = list(by_id.items())[: max(1, min(limit, 15))]
    out = []
    if targets:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {pool.submit(_ataw_detail_rows, rid, b): rid for rid, b in targets}
            for fut in as_completed(futs):
                try:
                    out.extend(fut.result())
                except Exception:
                    pass
    if not out and biz_errors and not by_id:
        raise RuntimeError("ataw: " + "; ".join(biz_errors[:3]))
    return out, len(by_id)


def has_usable_urls(items: list) -> bool:
    return any(is_share_url((r.get("url") or "")) for r in items)


def should_ataw_fallback(engines: list, items: list) -> bool:
    if "ataw" in engines:
        return False
    if not DEFAULT_ENGINES.intersection(engines):
        return False
    return not has_usable_urls(items)


def finalize_items(items: list, args, cloud_types, include, exclude) -> list:
    seen = set()
    merged = []
    for rec in items:
        if not pass_filters(rec, include, exclude, cloud_types):
            continue
        key = norm_url(rec["url"])
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(rec)
    merged.sort(key=lambda r: r.get("datetime") or "", reverse=True)
    merged.sort(key=lambda r: (-kw_hits(r, args.kw), ENGINE_RANK.get(r.get("source") or "", 5)))
    dead = load_dead_links()
    if dead:
        merged.sort(key=lambda r: norm_url(r["url"]) in dead)
    if args.limit > 0:
        counts = {}
        trimmed = []
        for rec in merged:
            c = rec["cloud"]
            counts[c] = counts.get(c, 0) + 1
            if counts[c] <= args.limit:
                trimmed.append(rec)
        merged = trimmed
    return merged


def norm_url(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    try:
        p = urllib.parse.urlsplit(u)
        return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))
    except Exception:
        return u.lower()


def pass_filters(rec: dict, include, exclude, cloud_types) -> bool:
    if cloud_types and rec["cloud"] not in {canon_type(x) for x in cloud_types}:
        return False
    blob = " ".join([rec.get("note") or "", rec.get("url") or ""]).lower()
    if include and not all(x.lower() in blob for x in include):
        return False
    if exclude and any(x.lower() in blob for x in exclude):
        return False
    return bool(rec.get("url"))


def fmt_date(dt: str) -> str:
    if not dt or dt.startswith("0001"):
        return ""
    for fmt, n in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(dt[:n], fmt).strftime("%Y-%m-%d")
        except (ValueError, IndexError):
            pass
    return dt[:10]


ENGINE_ORDER = ("pansou", "haisou", "yunso", "ataw", "panxiaozi", "movie", "local")

# 来源等级（借鉴 PanSou「插件等级」排序维度）：主源 0，补充源依次降级
ENGINE_RANK = {"pansou": 0, "haisou": 0, "yunso": 0, "local": 0, "ataw": 1, "panxiaozi": 1, "movie": 2}


def kw_hits(rec: dict, kw: str) -> int:
    """关键词匹配度：note 命中关键词分词的数量越多越靠前。"""
    note = (rec.get("note") or "")
    toks = [t for t in re.split(r"[\s,，]+", (kw or "").strip()) if t]
    if not toks:
        return 0
    return sum(1 for t in toks if t in note)


def kw_variants(kw: str) -> list[str]:
    """CJK 关键词变体（借鉴 PanHub searchKeyword 思路）：去空格、全角转半角、去修饰词。"""
    base = (kw or "").strip()
    if not base:
        return []
    out = []
    no_space = re.sub(r"\s+", "", base)
    if no_space and no_space != base:
        out.append(no_space)
    half = no_space.translate(str.maketrans({chr(0xFF01 + i): chr(0x21 + i) for i in range(94)} | {"\u3000": " "}))
    if half and half not in out and half != base:
        out.append(half)
    bare = re.sub(r"(全集|合集|4k|1080p|720p|高清|国粤|双语)", "", base, flags=re.I).strip()
    if bare and bare not in out and bare != base:
        out.append(bare)
    return out[:3]

CLOUD_ORDER = (
    "quark", "aliyun", "baidu", "xunlei", "uc", "115", "tianyi",
    "mobile", "123", "lanzou", "pikpak", "guangya", "magnet", "ed2k", "others",
)


def format_text(kw: str, items: list, errors: list, totals: dict) -> str:
    lines = ["搜索关键词: %s" % kw]
    if totals:
        bits = ["%s %s 条" % (k, totals[k]) for k in ENGINE_ORDER if k in totals]
        bits += ["%s %s 条" % (k, v) for k, v in totals.items() if k not in ENGINE_ORDER]
        lines.append("各源命中: " + "；".join(bits))
    lines.append("去重后 %s 条" % len(items))
    lines.append("")
    for e in errors:
        lines.append("来源失败: " + e)
    if errors:
        lines.append("")
    if not items:
        lines.append("未找到相关资源。可换中文片名、英文原名，或加 --engine 指定来源。")
        return "\n".join(lines)

    grouped = {}
    for rec in items:
        grouped.setdefault(rec["cloud"], []).append(rec)
    clouds = [c for c in CLOUD_ORDER if c in grouped] + [c for c in grouped if c not in CLOUD_ORDER]
    for cloud in clouds:
        rows = grouped[cloud]
        lines.append("【%s】(%s)" % (CLOUD_NAMES.get(cloud, cloud), len(rows)))
        for i, rec in enumerate(rows, 1):
            lines.append("  %s. %s" % (i, rec.get("note") or "无标题"))
            lines.append("     链接: %s" % rec["url"])
            lines.append("     提取码: %s" % (rec.get("password") or "无"))
            meta = []
            if rec.get("source"):
                meta.append(rec["source"])
            d = fmt_date(rec.get("datetime") or "")
            if d:
                meta.append(d)
            if rec.get("size"):
                try:
                    meta.append("%.2f GB" % (float(rec["size"]) / (1024 ** 3)))
                except (TypeError, ValueError):
                    pass
            if rec.get("files"):
                meta.append("%s 个文件" % rec["files"])
            if rec.get("hot"):
                meta.append("热门")
            if meta:
                lines.append("     来源: " + " | ".join(meta))
            lines.append("")
    return "\n".join(lines)


def build_jobs(args, engines, cloud_types, include, exclude):
    jobs = {}
    if "pansou" in engines:
        jobs["pansou"] = lambda: search_pansou(
            PANSOU_PUBLIC, args.kw, cloud_types, include, exclude, args.src, args.refresh, "pansou",
            getattr(args, "pansou_timeout", 45.0)
        )
    if "haisou" in engines:
        jobs["haisou"] = lambda: search_haisou(
            args.kw, cloud_types, args.page, args.page_size, args.scope, args.min_size, args.max_size
        )
    if "yunso" in engines:
        jobs["yunso"] = lambda: search_yunso(args.kw, args.page, args.yunso_mode)
    if "panxiaozi" in engines:
        jobs["panxiaozi"] = lambda: search_panxiaozi(args.kw, args.limit)
    if "ataw" in engines:
        jobs["ataw"] = lambda: search_ataw(args.kw, args.limit)
    if "movie" in engines:
        jobs["movie"] = lambda: search_movie(args.kw, args.page, args.page_size)
    if "local" in engines:
        base = local_pansou_base()
        if base:
            jobs["local"] = lambda b=base: search_pansou(
                b, args.kw, cloud_types, include, exclude, args.src, args.refresh, "local",
                getattr(args, "pansou_timeout", 45.0)
            )
        else:
            def _no_local():
                raise RuntimeError("未检测到本地 PanSou（PANSOU_URL / NETDISK_API_URL / :8888）")

            jobs["local"] = _no_local
    return jobs


def load_engine_state() -> dict:
    try:
        with open(ENGINE_STATE_PATH, encoding="utf-8-sig") as f:  # utf-8-sig 容错 PowerShell 写入的 BOM
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def save_engine_state(st: dict) -> None:
    try:
        os.makedirs(os.path.dirname(ENGINE_STATE_PATH), exist_ok=True)
        with open(ENGINE_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def engine_blocked(name: str, st: dict) -> bool:
    """连续 ENGINE_FAIL_LIMIT 次失败后冷却 ENGINE_COOLDOWN_MIN 分钟（PanSeek 插件熔断的脚本化移植）。"""
    e = st.get(name) or {}
    if int(e.get("fails") or 0) < ENGINE_FAIL_LIMIT:
        return False
    try:
        last = datetime.fromisoformat(e.get("last_fail") or "")
    except ValueError:
        return False
    return (datetime.now() - last) < timedelta(minutes=ENGINE_COOLDOWN_MIN)


def load_dead_links() -> set:
    """已确认失效的链接集合（读 check_links 四级状态机文件）。
    借鉴 quark-auto-save「记录失效分享并跳过」：只降权不删除，仍会展示。"""
    try:
        with open(LINK_STATE_PATH, encoding="utf-8-sig") as f:
            d = json.load(f)
        return {norm_url(u) for u, r in d.items() if isinstance(r, dict) and r.get("state") == "bad"}
    except (OSError, ValueError):
        return set()


def _search_once(args, engines, cloud_types, include, exclude, kw=None, elapsed=None):
    if kw and kw != args.kw:
        ns = argparse.Namespace(**vars(args))
        ns.kw = kw
        args = ns
    jobs = build_jobs(args, engines, cloud_types, include, exclude)
    by_name = {}
    errors = []
    totals = {}
    t_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        futs = {pool.submit(fn): name for name, fn in jobs.items()}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                rows, total = fut.result()
                totals[name] = total
                by_name[name] = rows
            except Exception as e:
                errors.append("%s: %s" % (name, e))
            if elapsed is not None:
                elapsed[name] = round(time.monotonic() - t_start, 1)

    # 熔断状态回写：成功清零，失败累计（借鉴 PanSeek 插件熔断）
    st = load_engine_state()
    changed = False
    for name in jobs:
        if name in by_name:
            changed |= st.pop(name, None) is not None
        elif any(x.startswith(name + ":") for x in errors):
            e = st.get(name) or {}
            e["fails"] = int(e.get("fails") or 0) + 1
            e["last_fail"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            st[name] = e
            changed = True
    if changed or st:
        save_engine_state(st)

    merged = []
    for name in ENGINE_ORDER:
        merged.extend(by_name.get(name) or [])

    items = finalize_items(merged, args, cloud_types, include, exclude)
    return items, totals, errors


def run(args):
    engines = [e.strip() for e in (args.engine or "pansou,haisou,yunso,ataw").split(",") if e.strip()]
    if "all" in engines:
        # all = 默认四源（含 ataw），且保留同批次显式指定的其它引擎（如 all,panxiaozi）
        engines = ["pansou", "haisou", "yunso", "ataw"] + [e for e in engines if e not in ("all", "pansou", "haisou", "yunso", "ataw")]
    cloud_types = split_csv(args.cloud_types)
    include = split_csv(args.include)
    exclude = split_csv(args.exclude)

    # 引擎熔断：近期连续失败的引擎本次跳过（--fresh 强制全跑）
    st = load_engine_state()
    skipped = []
    if not getattr(args, "fresh", False):
        for e in list(engines):
            if engine_blocked(e, st):
                engines.remove(e)
                skipped.append(e)

    elapsed = {}
    items, totals, errors = _search_once(args, engines, cloud_types, include, exclude, elapsed=elapsed)

    # 关键词变体兜底（借鉴 PanHub searchKeyword 思路）：0结果时用变体重试一轮
    variant_used = ""
    if not items and not getattr(args, "no_variants", False):
        for v in kw_variants(args.kw):
            v_items, v_totals, v_errors = _search_once(args, engines, cloud_types, include, exclude, kw=v, elapsed=elapsed)
            if v_items:
                variant_used = v
                items = v_items
                totals = v_totals
                errors = errors + ["变体「%s」: %s" % (v, e) for e in v_errors]
                break

    ataw_fallback = False
    if should_ataw_fallback(engines, items):
        try:
            fb_rows, fb_total = search_ataw(args.kw, args.limit)
            if fb_rows:
                ataw_fallback = True
                totals["ataw"] = fb_total
                items = finalize_items(items + fb_rows, args, cloud_types, include, exclude)
        except Exception as e:
            errors.append("ataw(fallback): %s" % e)

    errors.sort()
    result = {
        "keyword": args.kw,
        "total": len(items),
        "totals": totals,
        "errors": errors,
        "results": items,
    }
    if ataw_fallback:
        result["ataw_fallback"] = True
    if elapsed:
        result["elapsed"] = elapsed
    if skipped:
        result["skipped_engines"] = skipped
    if variant_used:
        result["variant_used"] = variant_used
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if skipped:
            print("熔断跳过: %s（--fresh 强制重试）" % "、".join(skipped))
        if elapsed:
            slow = sorted(elapsed.items(), key=lambda x: -x[1])
            print("各源耗时: " + "；".join("%s %.1fs" % (k, v) for k, v in slow))
        print(format_text(args.kw, items, errors, totals))
        if variant_used:
            print("（0 结果已自动用变体「%s」重试）" % variant_used)
        if ataw_fallback:
            print("（默认源无可用直链，已自动尝试 TA搜 ataw 兜底）")
    return 0 if items or not errors else 1


def main():
    p = argparse.ArgumentParser(description="多源网盘资源搜索")
    p.add_argument("--kw", required=True, help="搜索关键词")
    p.add_argument("--cloud_types", help="网盘类型，逗号分隔，如 quark,aliyun,baidu")
    p.add_argument("--include", help="结果须含这些词，逗号分隔")
    p.add_argument("--exclude", help="排除这些词，逗号分隔")
    p.add_argument("--src", default="plugin", choices=["all", "tg", "plugin"], help="盘搜数据源，默认 plugin（更快更稳）")
    p.add_argument("--engine", default="pansou,haisou,yunso,ataw", help="pansou,haisou,yunso,ataw,panxiaozi,movie,local；all=默认四源（含 ataw）；ataw 已是默认引擎，显式排除后默认源无果时仍会自动兜底")
    p.add_argument("--scope", default="title", choices=["title", "files"], help="海搜范围")
    p.add_argument("--yunso_mode", default="90001", choices=["90001", "90002"], help="小云搜索：90001智能 / 90002精准")
    p.add_argument("--min_size", type=float, help="海搜最小体积 GB")
    p.add_argument("--max_size", type=float, help="海搜最大体积 GB")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--page_size", type=int, default=10)
    p.add_argument("--limit", type=int, default=8, help="每种网盘最多展示条数")
    p.add_argument("--refresh", action="store_true", help="盘搜绕过缓存")
    p.add_argument("--pansou_timeout", type=float, default=45.0, help="盘搜请求超时秒数（默认 45；急用可调小，代价是聚合不全、结果变少）")
    p.add_argument("--fresh", action="store_true", help="忽略引擎熔断状态，强制全引擎执行")
    p.add_argument("--no-variants", action="store_true", help="禁用 0 结果时的关键词变体自动重试")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
