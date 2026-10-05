#!/usr/bin/env python3
"""网盘链接检测（纯标准库）。

检测策略（诚实、不编造）：
  1. 夸克链接：若环境提供夸克 CLI（QUARK_SKILL_DIR + NODE_BIN），用 `share-detail` 实测，
     返回 code=0 且有 files 即「有效」，否则按 msg 标「失效」。这是本沙箱唯一可靠的实时核验。
  2. 自建 PanSou：POST /api/check/links（需部署 PANSOU_URL / :8888）。
  3. 其它公开链接：内置 8 类匿名官方/页面检测器（借鉴 fish2018/NetDiskLinkValidator，无需
     cookie/token）覆盖夸克/阿里/115/123/天翼/百度/蓝奏/UC；不支持的类型仍标「未核验」，
     由 Agent 向用户说明，不假装核验。
  4. 本地四级状态机（借鉴 supansou/DuPanSou-Archive）：有效/疑似失效/确认失效/未核验。
     连续 2 次失败才判死（防误杀）；有效 72h 内不复检，疑似 30min 后才复检，
     失效 12h 后复查是否恢复；磁力/电驴等不可检类型永不误伤；
     检测异常（uncertain）不计入失败。状态存本地 JSON（默认 ~/.pan_search/link_state.json）。
公开盘搜本身不当检测服务，绝不编造有效/失效。

--file 行格式（v1.7.7）：`URL`、`URL<TAB>提取码` 或 `URL 提取码`（逐链提取码，优先于全局 --password）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DISK_PATTERNS = {
    "quark": r"pan\.quark\.cn",
    "baidu": r"pan\.baidu\.com",
    "aliyun": r"www\.alipan\.com|aliyundrive\.com",
    "xunlei": r"pan\.xunlei\.com",
    "115": r"115\.com|115cdn\.com",
    "tianyi": r"cloud\.189\.cn",
    "uc": r"drive\.uc\.cn|fast\.uc\.cn",
    "mobile": r"caiyun\.139\.com|yun\.139\.com",
    "123": r"123pan\.com|123912\.com",
    "pikpak": r"mypikpak\.com",
    "lanzou": r"lanzou[a-z]?\.com",
    "magnet": r"^magnet:",
    "ed2k": r"^ed2k:",
}

CLOUD_NAMES = {
    "quark": "夸克网盘", "baidu": "百度网盘", "aliyun": "阿里云盘", "xunlei": "迅雷网盘",
    "115": "115网盘", "tianyi": "天翼云盘", "uc": "UC网盘", "mobile": "移动云盘",
    "123": "123网盘", "pikpak": "PikPak", "lanzou": "蓝奏云", "magnet": "磁力链接",
    "ed2k": "电驴链接", "others": "其他",
}

STATUS_ICON = {
    "ok": "[有效]", "suspect": "[疑似失效]", "bad": "[确认失效]", "locked": "[需提取码]",
    "unsupported": "[不支持检测]", "uncertain": "[未核验]",
}

# 统一状态码（借鉴 owu/share-sniffer 输出契约：0 有效 / 1 疑似 / 2 失效 / 3 需码 / 4 不支持 / 5 未核验）
STATE_CODE = {"ok": 0, "suspect": 1, "bad": 2, "locked": 3, "unsupported": 4, "uncertain": 5}

# 四级状态机复检策略（借鉴 supansou/DuPanSou-Archive）
STATE_OK_TTL = 72 * 3600        # 有效 72h 内不复检
STATE_SUSPECT_TTL = 30 * 60     # 疑似失效 30min 内不重复检
STATE_BAD_RECHECK = 12 * 3600   # 确认失效 12h 后复查是否恢复
FAILS_TO_CONFIRM = 2            # 连续 2 次失败才判死
DEFAULT_STATE_PATH = os.path.join(os.path.expanduser("~"), ".pan_search", "link_state.json")


def ssl_ctx():
    c = ssl.create_default_context()
    try:
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
    except Exception:
        pass
    return c


def http_json(url, *, method="GET", body=None, timeout=30):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    hdrs = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (compatible; PanSearch-Skill/1.0)"}
    if data is not None:
        hdrs["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, context=ssl_ctx(), timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def detect_api(explicit=None):
    cands = []
    if explicit:
        cands.append(explicit.rstrip("/"))
    for env in (os.environ.get("PANSOU_URL"), os.environ.get("NETDISK_API_URL")):
        if env:
            cands.append(env.rstrip("/"))
    cands.append("http://127.0.0.1:8888")
    seen = []
    for url in cands:
        if url in seen:
            continue
        seen.append(url)
        try:
            http_json(url + "/api/health", timeout=4)
            return url
        except Exception:
            continue
    return None


def detect_quark(skill_dir=None, node_bin=None):
    sd = (skill_dir or os.environ.get("QUARK_SKILL_DIR") or "").strip()
    nb = (node_bin or os.environ.get("NODE_BIN") or "").strip()
    if not sd or not nb:
        return None
    cli = os.path.join(sd, "scripts", "quark-drive.cjs")
    if not (os.path.isfile(cli) and shutil.which(nb)):
        return None
    return nb, cli


def detect_disk_type(url: str) -> str:
    for dtype, pattern in DISK_PATTERNS.items():
        if re.search(pattern, url, re.I):
            return dtype
    return "others"


def verify_quark(node_bin, cli, url, password=""):
    cmd = [node_bin, cli, "share-detail", "--url", url]
    if password:
        cmd += ["--passcode", password]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                              encoding="utf-8", errors="replace")
    except Exception as e:
        return "uncertain", "夸克 CLI 调用失败: %s" % e
    raw = (out.stdout or "").strip()
    if not raw:
        return "uncertain", (out.stderr or "夸克 CLI 无输出").strip()[:200]
    try:
        d = json.loads(raw)
    except Exception:
        return "uncertain", "夸克 CLI 输出非 JSON: %s" % raw[:120]
    code = d.get("code")
    msg = d.get("msg") or ""
    data = d.get("data") or {}
    if code == 0 and data.get("files") is not None:
        n = data.get("file_count") or (len(data.get("files") or []) if isinstance(data.get("files"), list) else 0)
        return "ok", "夸克 CLI 实测有效（%s 个文件）" % n
    return "bad", "夸克 CLI: %s" % msg


# ---- 内置匿名检测器（借鉴 fish2018/NetDiskLinkValidator；纯标准库、无需 cookie/token） ----
BROWSER_HDRS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def http_api(url, *, method="GET", body=None, timeout=25):
    """匿名 JSON 接口请求（浏览器 UA）。"""
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    hdrs = dict(BROWSER_HDRS)
    if data is not None:
        hdrs["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, context=ssl_ctx(), timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def http_text(url, *, timeout=25):
    """匿名 HTML 页面抓取（浏览器 UA）。"""
    req = urllib.request.Request(url, headers=dict(BROWSER_HDRS))
    with urllib.request.urlopen(req, context=ssl_ctx(), timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def http_api_tolerant(url, *, method="GET", body=None, timeout=25):
    """返回 (json_or_None, http_code_or_None)：4xx 也尝试读 JSON body（夸克/阿里 API 的业务错误走 HTTP 4xx）。"""
    try:
        return http_api(url, method=method, body=body, timeout=timeout), None
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", errors="replace")), e.code
        except Exception:
            return None, e.code


def _sid(pattern, url):
    m = re.search(pattern, url, re.I)
    return (m.group(1) or "") if m else ""


def verify_aliyun(url, password=""):
    sid = _sid(r"(?:alipan\.com|aliyundrive\.com)/s/([A-Za-z0-9]+)", url)
    if not sid:
        return "uncertain", "未能识别阿里分享 id"
    d, _hc = http_api_tolerant("https://api.aliyundrive.com/adrive/v3/share_link/get_share_by_anonymous",
                               method="POST", body={"share_id": sid}, timeout=25)
    if not isinstance(d, dict):
        return "uncertain", "阿里匿名接口无有效响应"
    if "ShareLink" in str(d.get("code") or ""):
        return "bad", "阿里匿名接口: 分享不存在/已取消"
    if d.get("has_pwd"):
        return "locked", "阿里匿名接口: 需提取码"
    if d.get("file_infos"):
        return "ok", "阿里匿名接口实测有效（%s 个文件）" % len(d["file_infos"])
    return "uncertain", "阿里匿名接口返回无法判定"


def verify_115(url, password=""):
    sc = _sid(r"(?:115\.com|115cdn\.com)/s/([A-Za-z0-9]+)", url)
    rc = _sid(r"[?&]password=([A-Za-z0-9]+)", url) or password
    if not sc:
        return "uncertain", "未能识别 115 分享码"
    qs = urllib.parse.urlencode({"share_code": sc, "receive_code": rc})
    d = http_api("https://webapi.115.com/share/snap?" + qs, timeout=20)
    if d.get("state") is True:
        return "ok", "115 匿名接口实测有效"
    err = str(d.get("error") or "")
    if "访问码" in err or "提取码" in err:
        return "locked", "115: %s" % err
    return "bad", "115: %s" % (err or "接口判定分享不可用")


def verify_123(url, password=""):
    sk = _sid(r"123pan\.com/s/([A-Za-z0-9]+)", url)
    if not sk:
        return "uncertain", "未能识别 123 分享 key"
    d = http_api("https://www.123pan.com/api/share/info?shareKey=" + urllib.parse.quote(sk), timeout=20)
    if str(d.get("code")) != "0":
        msg = str(d.get("message") or d.get("msg") or "")
        if "密码" in msg or "提取" in msg:
            return "locked", "123: %s" % msg
        return "bad", "123: %s" % (msg or "接口判定分享不可用")
    info = d.get("data") or {}
    if info.get("Code") not in (None, 0, "0"):
        return "bad", "123: 分享已失效"
    return "ok", "123 匿名接口实测有效"


def verify_tianyi(url, password=""):
    code = _sid(r"cloud\.189\.cn/(?:t/|web/share\?code=)([A-Za-z0-9]+)", url)
    if not code:
        return "uncertain", "未能识别天翼分享码"
    d = http_api("https://cloud.189.cn/api/open/share/getShareInfoByCodeV2",
                 method="POST", body={"shareCode": code}, timeout=25)
    blob = json.dumps(d, ensure_ascii=False)
    if "ShareExpiredError" in blob or "SHARE_EXPIRED" in blob or "已失效" in blob:
        return "bad", "天翼: 分享已失效"
    if d.get("fileInfos") or d.get("shareInfo"):
        n = len(d.get("fileInfos") or [])
        return "ok", "天翼匿名接口实测有效%s" % ("（%s 个文件）" % n if n else "")
    return "uncertain", "天翼匿名接口返回无法判定"


def verify_baidu(url, password=""):
    if not _sid(r"pan\.baidu\.com/s/([A-Za-z0-9_\-]+)", url):
        return "uncertain", "未能识别百度分享 id"
    page = http_text(url, timeout=25)
    if ("分享的文件已经被取消" in page) or ("分享已过期" in page) or ("你访问的页面不存在" in page):
        return "bad", "百度页面判定: 分享失效"
    if ("请输入提取码" in page) or ("请输入提取密码" in page):
        return "locked", "百度: 需提取码"
    if "分享" in page and "网盘" in page:
        return "ok", "百度页面判定: 分享可访问"
    return "uncertain", "百度页面无法明确判定"


def verify_lanzou(url, password=""):
    page = http_text(url, timeout=20)
    if ("文件取消保存" in page) or ("文件不存在" in page) or ("来晚一步" in page):
        return "bad", "蓝奏页面判定: 文件失效"
    return "ok", "蓝奏页面判定: 可访问"


def verify_uc(url, password=""):
    page = http_text(url, timeout=25)
    if ("分享的内容太火爆" in page) or ("已失效" in page) or ("已经被取消" in page) or ("链接不存在" in page):
        return "bad", "UC 页面判定: 分享失效"
    if ("提取码" in page) or ("访问码" in page):
        return "locked", "UC: 需提取码"
    return "uncertain", "UC 页面无法明确判定"


def verify_quark_anon(url, password=""):
    pid = _sid(r"pan\.quark\.cn/s/([a-f0-9]+)", url)
    if not pid:
        return "uncertain", "未能识别夸克分享 id"
    d, _hc = http_api_tolerant(
        "https://drive.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc",
        method="POST", body={"pwd_id": pid, "passcode": password or ""}, timeout=25)
    if not isinstance(d, dict):
        return "uncertain", "夸克匿名接口无有效响应"
    msg = str(d.get("message") or "")
    stoken = (d.get("data") or {}).get("stoken")
    if not stoken:
        if ("提取码" in msg) or ("访问码" in msg):
            return "locked", "夸克匿名接口: %s" % (msg or "需要提取码")
        if ("不存在" in msg) or ("已失效" in msg) or ("已取消" in msg) or ("删除" in msg):
            return "bad", "夸克匿名接口: %s" % msg
        return "uncertain", "夸克匿名接口: %s" % (msg or "未取得 stoken")
    # token 接口返回 stoken 即证明分享存活（失效分享在此步即报「分享不存在」），无需再查 detail
    return "ok", "夸克匿名接口实测有效（stoken 获取成功）"


ANON_DISPATCH = {
    "quark": verify_quark_anon, "aliyun": verify_aliyun, "115": verify_115,
    "123": verify_123, "tianyi": verify_tianyi, "baidu": verify_baidu,
    "lanzou": verify_lanzou, "uc": verify_uc,
}


def verify_anon(url, dtype, password=""):
    """内置匿名检测入口；不支持的类型或检测异常一律 honest 返回 uncertain（不计入失败）。"""
    fn = ANON_DISPATCH.get(dtype)
    if not fn:
        return "uncertain", "该网盘类型暂无内置匿名检测（不假装核验）"
    try:
        return fn(url, password or "")
    except Exception as e:
        return "uncertain", "内置匿名检测失败: %s" % e


# 行内提取码只认 2–16 位字母数字（可带「提取码:」等前缀）；`URL 片名` 这类附注不会被误当提取码，`URL 提取码:` 空值也不会把冒号当码
_FILE_PWD_RE = re.compile(
    r"^(?:(?:提取码|提取密码|访问码|访问密码|密码|pwd|password|passcode|code)\s*[:：=]?\s*)?([A-Za-z0-9]{2,16})(?=\s|#|$)", re.I)


def parse_link_line(line, default_pwd=""):
    """解析 --file 的一行：`URL` / `URL<TAB>pwd` / `URL pwd`（也容忍 `URL 提取码:abcd`）。
    返回 (url, pwd) 或 None（空行/注释）。行内无提取码时回落到全局 --password。"""
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(None, 1)
    url = parts[0]
    pwd = ""
    if len(parts) > 1:
        m = _FILE_PWD_RE.match(parts[1].strip())
        if m:
            pwd = m.group(1)
    return url, (pwd or default_pwd or "")


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_state(path, state):
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def state_decision(rec, now, force=False):
    """复检策略：返回 ("check", None) 需实测，或 ("cache", rec) 沿用缓存。"""
    if force or not isinstance(rec, dict) or not rec.get("state"):
        return "check", None
    st = rec.get("state")
    try:
        age = now - float(rec.get("last_checked") or 0)
    except Exception:
        return "check", None
    if st == "ok" and age < STATE_OK_TTL:
        return "cache", rec
    if st == "suspect" and age < STATE_SUSPECT_TTL:
        return "cache", rec
    if st == "bad" and age < STATE_BAD_RECHECK:
        return "cache", rec
    return "check", None


def state_update(rec, verdict, now, summary=""):
    """verdict: ok / bad / uncertain。uncertain 是检测异常，不计入失败（防误杀）。"""
    rec = dict(rec or {})
    rec["last_checked"] = now
    if verdict == "ok":
        rec["state"] = "ok"
        rec["fail_count"] = 0
        rec["last_ok"] = now
        if summary:
            rec["summary"] = summary
    elif verdict == "bad":
        fc = int(rec.get("fail_count") or 0) + 1
        rec["fail_count"] = fc
        rec["state"] = "bad" if fc >= FAILS_TO_CONFIRM else "suspect"
        if summary:
            rec["summary"] = summary
    else:
        rec.setdefault("state", "uncertain")
        rec["uncertain_count"] = int(rec.get("uncertain_count") or 0) + 1
    return rec


def main():
    p = argparse.ArgumentParser(description="网盘链接检测（诚实标注，不编造）")
    p.add_argument("--url", action="append", help="待检测链接，可重复")
    p.add_argument("--file", help="每行一个链接，# 开头为注释；可写 `URL<TAB>提取码` 或 `URL 提取码`（逐链提取码）")
    p.add_argument("--type", help="网盘类型；默认从 URL 识别")
    p.add_argument("--password", default="", help="提取码（全局默认；--file 行内提取码优先）")
    p.add_argument("--proxy", help="检测代理 socks5://...")
    p.add_argument("--api", help="PanSou API 根地址")
    p.add_argument("--quark_skill_dir", help="夸克 CLI skill 目录（或设 QUARK_SKILL_DIR）")
    p.add_argument("--node_bin", help="node 可执行（或设 NODE_BIN）")
    p.add_argument("--json", action="store_true")
    p.add_argument("--state", help="状态机 JSON 路径（默认 ~/.pan_search/link_state.json；或设 LINK_STATE_PATH）")
    p.add_argument("--no-state", action="store_true", help="本次不读写状态机，全部实测、不缓存")
    p.add_argument("--force", action="store_true", help="忽略复检策略，全部强制实测")
    args = p.parse_args()

    pairs = [(u, args.password) for u in (args.url or [])]
    if args.file:
        with open(args.file, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                parsed = parse_link_line(line, args.password)
                if parsed:
                    pairs.append(parsed)
    if not pairs:
        p.print_help()
        print("\n请提供 --url 或 --file", file=sys.stderr)
        sys.exit(2)

    state_path = None
    if not args.no_state:
        state_path = args.state or os.environ.get("LINK_STATE_PATH") or DEFAULT_STATE_PATH
    store = load_state(state_path) if state_path else {}
    now = time.time()
    t0 = now

    api = detect_api(args.api)
    quark = detect_quark(args.quark_skill_dir, args.node_bin)

    rows = []
    for url, pwd in pairs:
        dtype = args.type or detect_disk_type(url)
        if dtype in ("magnet", "ed2k"):
            rows.append({"disk_type": dtype, "url": url, "password": pwd,
                         "_via": "skip", "state": "unsupported",
                         "summary": "磁力/电驴链接不做有效性检测（永不误伤）"})
            continue
        act, cached = state_decision(store.get(url) or {}, now, args.force)
        if act == "cache":
            age_h = (now - float(cached.get("last_checked") or 0)) / 3600
            rows.append({"disk_type": dtype, "url": url, "password": pwd,
                         "_via": "cache", "state": cached.get("state") or "uncertain",
                         "summary": "%s（状态机缓存 %.1fh 前实测，未复检；--force 强制）"
                                    % (cached.get("summary") or "", age_h),
                         "_cached": True})
            continue
        if dtype == "quark" and quark:
            state, summary = verify_quark(quark[0], quark[1], url, pwd)
            rows.append({"disk_type": dtype, "url": url, "password": pwd,
                         "_via": "direct", "state": state, "summary": summary})
        elif api:
            rows.append({"disk_type": dtype, "url": url, "password": pwd,
                         "_via": "api", "state": None, "summary": None})
        else:
            state, summary = verify_anon(url, dtype, pwd)
            rows.append({"disk_type": dtype, "url": url, "password": pwd,
                         "_via": "direct", "state": state, "summary": summary})

    # 有 api 时批量走自建 PanSou
    api_rows = [r for r in rows if r.get("_via") == "api"]
    if api_rows:
        body = {"items": [{"disk_type": r["disk_type"], "url": r["url"], "password": r["password"]} for r in api_rows]}
        if args.proxy:
            body["proxy_url"] = args.proxy
        try:
            result = http_json(api + "/api/check/links", method="POST", body=body, timeout=45)
            if isinstance(result, dict) and "code" in result and "data" in result:
                result = result.get("data") or result
            api_out = result.get("results") or result.get("data") or []
            for r, o in zip(api_rows, api_out if isinstance(api_out, list) else []):
                st = o.get("state") or o.get("status") or "uncertain"
                r["state"] = st
                r["summary"] = o.get("summary") or ""
        except Exception as e:
            for r in api_rows:
                r["state"] = "uncertain"
                r["summary"] = "检测接口失败(%s): %s" % (api, e)

    # 四级状态机：本次实测结果写回本地状态（uncertain 是检测异常，不计入失败）
    if state_path:
        changed = False
        for r in rows:
            st = r.get("state")
            if r.get("_via") in ("cache", "skip") or st not in ("ok", "bad", "uncertain"):
                continue
            verdict = st if st in ("ok", "bad") else "uncertain"
            rec = state_update(store.get(r["url"]), verdict, now, r.get("summary") or "")
            store[r["url"]] = rec
            r["state"] = rec.get("state") or st
            r["fail_count"] = rec.get("fail_count", 0)
            changed = True
        if changed:
            save_state(state_path, store)

    rows.sort(key=lambda r: (r.get("state") or "uncertain") != "ok",)
    ok = sum(1 for r in rows if r.get("state") == "ok")

    if args.json:
        for r in rows:
            r.pop("_via", None)
            r["cached"] = bool(r.pop("_cached", False))
            r["code"] = STATE_CODE.get(r.get("state") or "uncertain", 5)
        print(json.dumps({"total": len(rows), "ok": ok,
                          "elapsed_ms": int((time.time() - t0) * 1000),
                          "state_path": state_path or "", "results": rows},
                         ensure_ascii=False, indent=2))
        return

    print("检测 %s 条（夸克CLI:%s / 自建PanSou:%s / 状态机:%s）"
          % (len(rows), "有" if quark else "无", api or "无", state_path or "关"))
    for r in rows:
        state = r.get("state") or "uncertain"
        dt = r.get("disk_type") or detect_disk_type(r.get("url") or "")
        flag = " [缓存]" if r.get("_cached") else ""
        print("%s [%s]%s %s" % (STATUS_ICON.get(state, "[未核验]"), CLOUD_NAMES.get(dt, dt), flag, r.get("url", "")))
        if r.get("summary"):
            print("   %s" % r["summary"])
    print("有效 %s/%s" % (ok, len(rows)))


if __name__ == "__main__":
    main()
