#!/usr/bin/env python3
"""多源网盘资源搜索（仅标准库）。默认：公开盘搜 + 海搜 + 小云搜索 + TA搜；可选盘小子、影视库。

v1.8.0：内容级去重聚合（--all-dups 展开）、夸克内容智能按需抽查（--peek [smart|on|off]）、熔断生命周期自愈（冷却期满归零 + 成功归零）、错误消息脱敏截断、废弃引擎软校验与 pansou 50s 耗时保险丝。"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import math
import os
import random
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

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
SUPPORTED_ENGINES = frozenset({"pansou", "haisou", "yunso", "ataw", "panxiaozi", "movie", "local"})
SKILL_VERSION = "1.8.0"

PANSOU_TOTAL_TIMEOUT = 50.0  # 盘搜单次检索绝对超时硬上限（Monotonic 保险丝）

# 夸克内容智能按需抽查触发词（--peek smart 命中任一词时自动激活 Top 5 抽查）
PEEK_CONTENT_KEYWORDS = frozenset({
    # 文档与电子书类
    "小说", "电子书", "文档", "文本", "书单", "txt", "epub", "mobi", "pdf", "azw3",
    # 音频与有声类
    "有声", "有声书", "音频", "音乐", "无损", "flac", "mp3", "wav",
    # 图像与漫画类
    "漫画", "图集", "写真",
})

# 引擎熔断状态文件（借鉴 PanSeek「失败插件自动降级」；可用环境变量改路径）
ENGINE_STATE_PATH = os.environ.get("ENGINE_STATE_PATH") or os.path.expanduser("~/.pan_search/engine_state.json")
LINK_STATE_PATH = os.environ.get("LINK_STATE_PATH") or os.path.expanduser("~/.pan_search/link_state.json")
ENGINE_FAIL_LIMIT = 2    # 连续失败次数达到即冷却
ENGINE_COOLDOWN_MIN = 30  # 冷却时长（分钟）
# v1.7.7 风控熔断：HTTP 429/412 或验证码页（强风控）→ 一次即冷却 RISK_COOLDOWN_MIN 分钟（Retry-After 更长则从其，上限 24h）；
# HTTP 403 归为弱风控：仍按普通失败「连续 2 次 / 30 分钟」计数（403 也可能是 CDN 瞬时/配置问题），但本轮不再参与变体重试。
RISK_COOLDOWN_MIN = 30
RISK_COOLDOWN_MAX_MIN = 24 * 60
RISK_HTTP_CODES = (403, 412, 429)
RISK_STRONG_HTTP_CODES = (412, 429)
# 同站详情页抓取：并发上限与随机间隔（秒）
DETAIL_WORKERS = 2
DETAIL_DELAY = (0.2, 0.8)

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


def clip_err(msg: str, limit: int = 120) -> str:
    """引擎错误消息超长截断与脱敏（剥离 raw JSON 中的 debug/details 等杂质，单行整洁显示）。"""
    if not msg:
        return ""
    s = re.sub(r"[\r\n\t]+", " ", str(msg)).strip()

    # 剔除 JSON 中的 details / debug / stack / trace 等超长冗余字段
    s = re.sub(r',?\s*"(?:details|debug|stack|trace)"\s*:\s*\{.*?\}', '', s)
    s = re.sub(r',?\s*"(?:details|debug|stack|trace)"\s*:\s*"(?:\\.|[^"\\])*"', '', s)
    # 清理多余空括号与尾随逗号
    s = re.sub(r",\s*}", "}", s)
    s = re.sub(r"{\s*,", "{", s)
    s = re.sub(r"\s+", " ", s).strip()

    orig_len = len(s)
    if orig_len > limit:
        s = s[: limit - 10].rstrip() + f"…(共{orig_len}字符)"
    return s


def norm_title(note: str) -> str:
    """标题规范化清洗函数（用于同名同质资源两级去重聚合）：
    全半角统一、剥离外围装饰括注、正则剔除画质/状态/版本/渠道噪词，保留季数与显式年份。"""
    if not note:
        return ""
    s = html_lib.unescape(note)
    table = {chr(0xFF01 + i): chr(0x21 + i) for i in range(94)} | {"\u3000": " "}
    s = s.translate(str.maketrans(table))

    # 去掉文件后缀
    s = re.sub(r"\.(?:txt|epub|mobi|pdf|azw3|mp4|mkv|ts|flv|avi|mov|rmvb|wmv|iso|zip|rar|7z)$", "", s, flags=re.I)

    # 去掉常见的无意义包装前缀/后缀标签（如【夸克网盘】、【4K超清】等，但保留年份如 (2023) 或季数 (第一季)）
    def _clean_bracket(m):
        content = m.group(1).strip()
        if re.fullmatch(r"(?:19|20)\d{2}", content):
            return f"({content})"
        if re.search(r"(?:^|[^\w])(?:S\d+|第[一二三四五六七八九十\d]+季)(?:$|[^\w])", content, re.I):
            return f" {content} "
        return " "

    s = re.sub(r"[\[【（\(]([^\[\]\(\)【】（）]+)[\]】）\)]", _clean_bracket, s)

    # 画质与编码噪词 (注意使用 (?<![a-zA-Z0-9]) 和 (?![a-zA-Z0-9]) 避免 \b 与中文字符边界问题)
    s = re.sub(r"(?i)(?<![a-zA-Z0-9])(?:4k|8k|1080p|720p|2160p|hd|uhd|fhd|hevc|h\.?265|h\.?264|x265|x264|avc|remux|web-?dl|web-?rip|bdrip|bluray|blu-ray)(?![a-zA-Z0-9])", " ", s)
    s = re.sub(r"(?:超清|高清|高码率|高码|原盘|自制|修复版|珍藏版|蓝光|国粤双语|双语|国语|粤语|中字|双字|无字|熟肉|生肉|压制)", " ", s)

    # 状态与版本噪词
    s = re.sub(r"(?:完整版|未删减版|未删减|精校版|校对版|精校全本|全集|合集|全\d+集|全\d+期|更新至\d+集|更新至\d+期|更至\d+集|更至\d+期|更新至\d+|更至\d+|第\d+集|第\d+期|单文件)", " ", s)

    # 渠道与网盘词
    s = re.sub(r"(?:夸克网盘|夸克|阿里云盘|阿里|百度网盘|百度|迅雷网盘|迅雷|UC网盘|UC|115网盘|115|网盘资源|网盘|资源|分享|链接)", " ", s)

    # 清理多余空括号与标点
    s = re.sub(r"[\[\(（【]\s*[\]\)）】]", " ", s)
    s = re.sub(r"[\s\-_/\\|,，]+", " ", s).strip()

    return s if s else note.strip()


_quark_cli_detected = None
_quark_warned = False


def detect_quark() -> tuple[str, str] | None:
    """探测 node 与 quark-drive.cjs 路径：
    1. 环境变量 QUARK_SKILL_DIR
    2. 同级 .agent/skills/quarkclouddrive/scripts/quark-drive.cjs
    3. PATH / 系统默认位置
    返回 (node_bin, cjs_path) 或 None。
    """
    global _quark_cli_detected, _quark_warned
    if _quark_cli_detected is not None:
        return _quark_cli_detected

    import shutil
    node_bin = os.environ.get("NODE_BIN") or shutil.which("node") or "node"
    try:
        res = subprocess.run([node_bin, "-v"], capture_output=True, timeout=3)
        if res.returncode != 0:
            node_bin = None
    except Exception:
        node_bin = None

    if not node_bin:
        if not _quark_warned:
            sys.stderr.write("⚠️ 未检测到 Node.js 环境，夸克内容抽查 (--peek) 已自动降级为纯文本聚合\n")
            _quark_warned = True
        _quark_cli_detected = None
        return None

    candidates = []
    q_dir = os.environ.get("QUARK_SKILL_DIR")
    if q_dir:
        candidates.append(os.path.join(q_dir, "scripts", "quark-drive.cjs"))
        candidates.append(os.path.join(q_dir, "quark-drive.cjs"))

    curr_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.abspath(os.path.join(curr_dir, "..", "..", "quarkclouddrive", "scripts", "quark-drive.cjs")))
    candidates.append(os.path.abspath(os.path.join(curr_dir, "..", "..", "..", ".agent", "skills", "quarkclouddrive", "scripts", "quark-drive.cjs")))
    candidates.append(r"F:\TOOLs\AI's DATA\ABC\Quark\.agent\skills\quarkclouddrive\scripts\quark-drive.cjs")

    found_cjs = None
    for cand in candidates:
        if os.path.isfile(cand):
            found_cjs = cand
            break

    if not found_cjs:
        if not _quark_warned:
            sys.stderr.write("⚠️ 未找到 quark-drive.cjs，夸克内容抽查 (--peek) 已自动降级为纯文本聚合\n")
            _quark_warned = True
        _quark_cli_detected = None
        return None

    _quark_cli_detected = (node_bin, found_cjs)
    return _quark_cli_detected


def peek_quark_detail(url: str, pwd: str = "", pdir_fid: str = "0", size: int = 1) -> dict | None:
    quark = detect_quark()
    if not quark:
        return None
    node_bin, cjs_path = quark
    cmd = [node_bin, cjs_path, "share-detail", "--url", url, "--size", str(size), "--pdir-fid", str(pdir_fid)]
    if pwd:
        cmd.extend(["--passcode", pwd])
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=12)
        if proc.returncode != 0:
            return None
        out_str = proc.stdout.decode("utf-8", errors="replace")
        data = json.loads(out_str)
        if data.get("code") == 0 and "data" in data:
            return data["data"]
    except Exception:
        pass
    return None


def peek_quark(url: str, pwd: str = "") -> dict | None:
    """夸克分享内容轻量抽查：严格遵循实测校准注记。
    一级判定（免下钻 · 读 share_info 顶层元数据与聚合体积）：
      - 视频主判据：share_info.size > 100MB（聚合体积）；video_total > 0 为根直挂辅助判据；
      - 小说主判据：share_info.size < 5MB 且 title/first_file_name 含「作者：」；
      - 引流判据：扫码/小程序/全网资源等；
    二级判定（5MB~100MB 中间地带或单文件夹下钻首层文件列表）。
    """
    data = peek_quark_detail(url, pwd=pwd, pdir_fid="0", size=1)
    if not data:
        return None
    share_info = data.get("share_info") or {}
    token_info = data.get("token_info") or {}
    size_bytes = int(share_info.get("size") or 0)
    title = share_info.get("title") or token_info.get("title") or ""
    first_fid = share_info.get("first_fid") or ""
    video_total = int(share_info.get("video_total") or 0)
    first_file = share_info.get("first_file") or {}
    first_file_name = first_file.get("file_name") or ""
    is_dir = bool(first_file.get("dir"))

    drain_keywords = ("扫码", "小程序", "全网资源", "关注公众号", "防失联")
    if any(k in title or k in first_file_name for k in drain_keywords):
        return {"tag": "[内容:引流]", "text": f"{title} {first_file_name}"}

    # 一级判定：小说主判据
    if size_bytes < 5 * 1024 * 1024 and ("作者：" in title or "作者：" in first_file_name):
        return {"tag": "[内容:小说]", "text": f"{title} {first_file_name}"}

    # 一级判定：视频主判据（实测：剧集文件夹分享 video_total 恒为 0，聚合 size 为数十 GB）
    if size_bytes > 100 * 1024 * 1024 or (video_total > 0 and not is_dir):
        size_gb = size_bytes / (1024 ** 3)
        size_str = f"{size_gb:.1f}GB" if size_gb >= 1.0 else f"{size_bytes / (1024 ** 2):.0f}MB"
        return {"tag": f"[内容:视频 {size_str}]", "text": f"{title} {first_file_name}"}

    # 二级判定：5MB~100MB 中间地带或单文件夹下钻首层
    if first_fid and first_fid != "0":
        drill = peek_quark_detail(url, pwd=pwd, pdir_fid=first_fid, size=50)
        if drill:
            sub_files = drill.get("files") or []
            sub_names = [f.get("filename") or f.get("file_name") or "" for f in sub_files]
            combined_text = f"{title} {first_file_name} " + " ".join(sub_names)

            if any(k in combined_text for k in drain_keywords):
                return {"tag": "[内容:引流]", "text": combined_text}

            # 检查小说文件
            has_novel_ext = any(re.search(r"\.(?:txt|epub|mobi|azw3)$", n, re.I) for n in sub_names)
            if has_novel_ext:
                return {"tag": "[内容:小说]", "text": combined_text}

            # 检查视频文件
            has_video = any(re.search(r"\.(?:mp4|mkv|ts|flv|avi|mov|rmvb|wmv)$", n, re.I) for n in sub_names)
            has_ep = any(re.search(r"(?:EP?\d+|第\d+[集期])", n, re.I) for n in sub_names)
            if has_video or has_ep:
                size_gb = size_bytes / (1024 ** 3)
                size_str = f"{size_gb:.1f}GB" if size_gb >= 1.0 else f"{size_bytes / (1024 ** 2):.0f}MB"
                return {"tag": f"[内容:视频 {size_str}]" if size_bytes > 0 else "[内容:视频]", "text": combined_text}

            return {"tag": "", "text": combined_text}

    return {"tag": "", "text": f"{title} {first_file_name}"}


def should_peek(peek_mode: str, exclude: list[str] | None) -> bool:
    """是否激活夸克内容抽查：
    - off: 完全关闭
    - on: 强制开启
    - smart: 当 exclude 中命中内容题材关键词（小说/文档/txt/epub等）时自动激活
    """
    if peek_mode == "off":
        return False
    if peek_mode == "on":
        return True
    if not exclude:
        return False
    return any(x.lower().strip() in PEEK_CONTENT_KEYWORDS for x in exclude)


class RiskControlError(RuntimeError):
    """风控类错误（HTTP 429/412/403 或验证码/人机验证页）。仍是 RuntimeError，调用方无感。
    strong=True（429/412/验证码）一次即熔断；strong=False（403）按普通失败计数。"""

    def __init__(self, msg: str, retry_after: int = 0, strong: bool = True):
        super().__init__(msg)
        self.retry_after = int(retry_after or 0)
        self.strong = bool(strong)


RETRY_AFTER_MAX_SEC = 7 * 24 * 3600  # 解析上限（熔断时长另有 RISK_COOLDOWN_MAX_MIN 封顶）


def parse_retry_after(val) -> int:
    """Retry-After：秒数或 HTTP 日期 → 秒；无法解析返回 0。"""
    v = (val or "").strip() if isinstance(val, str) else ""
    if not v:
        return 0
    if re.fullmatch(r"[0-9]+", v):  # 只认 ASCII 数字（str.isdigit 会放行「²」等导致 int() 抛错）
        return min(int(v), RETRY_AFTER_MAX_SEC) if len(v) <= 12 else RETRY_AFTER_MAX_SEC
    try:
        dt = parsedate_to_datetime(v)
        if dt is None:
            return 0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return min(RETRY_AFTER_MAX_SEC, max(0, int((dt - datetime.now(timezone.utc)).total_seconds())))
    except (TypeError, ValueError, IndexError, OverflowError):
        return 0


# 标题须以验证类字样**开头**（如「验证码_哔哩哔哩」「百度安全验证」「Just a moment...」）；
# 搜索页常把关键词回显进标题（如盘小子「“验证码”搜索结果 - 盘小子」），故不做标题内任意位置匹配。
_CAPTCHA_TITLE_RE = re.compile(
    r"<title[^>]*>\s*(?:安全验证|人机验证|验证码|访问验证|百度安全验证|captcha|just a moment|attention required)", re.I)
_CAPTCHA_MARKERS = (
    "wappass.baidu.com/static/captcha", "/account/unhuman",
    "sec.douban.com/", "cf_chl_opt", "cf-browser-verification",
)


def looks_like_captcha(text: str) -> bool:
    """保守判定验证码/人机验证页：只看小页面的 <title> 开头与少量强特征（URL/脚本标识），避免误伤正常页与关键词回显。"""
    t = text or ""
    if len(t) > 60000:
        return False
    if any(m in t for m in _CAPTCHA_MARKERS):
        return True
    return bool(_CAPTCHA_TITLE_RE.search(t))


def _http_error(e: urllib.error.HTTPError, msg: str) -> RuntimeError:
    if e.code in RISK_HTTP_CODES:
        ra = parse_retry_after(e.headers.get("Retry-After") if e.headers else "")
        return RiskControlError(msg, ra, strong=e.code in RISK_STRONG_HTTP_CODES)
    return RuntimeError(msg)


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
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                if looks_like_captcha(raw):
                    raise RiskControlError("验证码/人机验证页面（风控）")
                raise

    try:
        return _open(ssl_context(insecure))
    except ssl.SSLError:
        if insecure:
            raise
        return _open(ssl_context(True))
    except TimeoutError as e:
        raise RuntimeError("请求超时") from e
    except urllib.error.HTTPError as e:
        try:
            body = e.read(8192)  # 只读开头：海搜 429 响应体实测约 20MB（debug 字段），全读纯浪费
        except Exception:
            body = b""
        detail = body.decode("utf-8", errors="replace")[:400]
        raise _http_error(e, "HTTP %s %s: %s" % (e.code, e.reason, detail)) from e
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
            text = resp.read().decode("utf-8", errors="replace")
        if looks_like_captcha(text):
            raise RiskControlError("验证码/人机验证页面（风控）")
        return text

    last_net_err = None
    for attempt in range(retries + 1):
        try:
            return _open(ssl_context(insecure))
        except ssl.SSLError:
            if insecure:
                raise
            return _open(ssl_context(True))
        except urllib.error.HTTPError as e:
            raise _http_error(e, "HTTP %s %s" % (e.code, e.reason)) from e
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


# ---- v1.7.7 文本直链提取（独立实现）：全角转半角、可无协议头、逐链就近配对提取码 ----
_FW_TRANS = str.maketrans({**{chr(0xFF01 + i): chr(0x21 + i) for i in range(94)}, "\u3000": " "})


def to_halfwidth(text: str) -> str:
    return (text or "").translate(_FW_TRANS)


# 各网盘「主机 + 分享路径」形态（必须带分享路径，主站入口/个人页不算）
_SHARE_HOSTPATH = (
    r"pan\.quark\.cn/s/[0-9A-Za-z]+",
    r"pan\.baidu\.com/(?:s/[0-9A-Za-z_\-]+|share/init\?surl=[0-9A-Za-z_\-]+)",
    r"(?:www\.)?(?:alipan|aliyundrive)\.com/s/[0-9A-Za-z]+",
    r"pan\.xunlei\.com/s/[0-9A-Za-z_\-]+",
    r"(?:drive|fast)\.uc\.cn/s/[0-9A-Za-z]+",
    r"(?:www\.)?(?:115|115cdn|anxia)\.com/s/[0-9A-Za-z]+",
    r"(?:h5\.)?cloud\.189\.cn/(?:t/[0-9A-Za-z]+|web/share\?code=[0-9A-Za-z]+|share\.html#/t/[0-9A-Za-z]+)",
    r"(?:yun|caiyun)\.139\.com/(?:shareweb/#/w/i/|w/i/|m/i\?)[0-9A-Za-z]+",
    r"(?:www\.)?(?:123pan\.com|123pan\.cn|123912\.com|123684\.com|123865\.com)/s/[0-9A-Za-z_\-]+",
    r"(?:[0-9A-Za-z\-]+\.)?(?:lanzou[a-z]?|lanzn)\.com/(?:tp/)?(?!u/)[0-9A-Za-z_]{5,}",
    r"(?:www\.)?mypikpak\.com/s/[0-9A-Za-z_\-]+",
)
SHARE_LINK_RE = re.compile(
    r"(?:(?:https?:)?//|(?<![A-Za-z0-9\-.@/]))(?:" + "|".join(_SHARE_HOSTPATH) + r")(?:[?#/&][A-Za-z0-9\-._~/?#=&%+]*)?"
    r"|magnet:\?xt=urn:btih:[0-9A-Za-z]{32,40}(?:&[A-Za-z0-9._]+=[A-Za-z0-9._%+\-:/]*)*",
    re.I,
)
_PWD_RE = re.compile(
    r"(?:提取码|提取密码|访问码|访问密码|分享码|密码|口令|(?<![A-Za-z])(?:pwd|passcode|password|code)(?![A-Za-z]))"
    r"\s*[\]】)」>]?\s*(?:[:=]|是|为)?\s*[\[【(「<]?\s*([A-Za-z0-9]{3,8})(?![A-Za-z0-9])",
    re.I,
)
PWD_WINDOW = 40  # 链接后多少字符内找提取码（且不越过下一条链接）
# 「(访问码: abcd): URL」式前置提取码：码后紧跟冒号再接链接，才认作该链接的码（无冒号/有「链接」字样时仍按链接后配对）
_PWD_PRE_RE = re.compile(_PWD_RE.pattern + r"\s*[\]】)」>]?\s*:\s*$", re.I)
_PWD_PRE_GAP_RE = re.compile(r"\s*[\]】)」>]?\s*:\s*$")


def _link_key(u: str) -> str:
    low = (u or "").strip()
    if low.lower().startswith("magnet:"):
        m = re.search(r"btih:([0-9A-Za-z]+)", low, re.I)
        return "magnet:" + (m.group(1).lower() if m else low.lower())
    low = re.sub(r"^(?:https?:)?//", "", low, flags=re.I)
    host, _, rest = low.partition("/")
    if "#" in rest and not re.search(r"139\.com|189\.cn", host, re.I):
        rest = rest.split("#", 1)[0]  # 夸克 #/list/share 等纯前端锚点不影响去重；移动/天翼的 # 是路径一部分
    rest = re.sub(r"(?:(?<=[?&])|^)(?:pwd|password|passcode)=[^&#]*&?", "", rest, flags=re.I)
    return host.lower() + "/" + rest.rstrip("/?&#")


def links_from_text(text: str) -> list[tuple[str, str]]:
    """从任意文本提取网盘分享链接 → [(url, pwd)]。
    全角转半角；识别带/不带 http(s) 的分享链接与磁力；截掉尾随标点/中文；
    提取码优先取 URL 的 pwd=/password=，否则取链接后 PWD_WINDOW 字符内最近的「提取码/密码/访问码/pwd/code」，
    且不越过下一条链接（逐链就近配对，不是全文共用一个码）；按规范化链接去重。"""
    return [(u, pwd) for u, pwd, _ in _scan_links(to_halfwidth(text))]


def _scan_links(t: str) -> list[tuple[str, str, int]]:
    """links_from_text 的核心（输入须已转半角，长度与原文逐字对应）→ [(url, pwd, 首次出现位置)]。"""
    spans = []
    for m in SHARE_LINK_RE.finditer(t):
        u = m.group(0)
        amp = u.find("&")
        if amp > 0 and "?" not in u[:amp] and not u.lower().startswith("magnet:"):
            u = u[:amp]  # 无 ? 的 & 不是查询串（多为 HTML/JSON 残留）
        u = u.rstrip(".?&#=/~")
        if u:
            spans.append((m.start(), m.start() + len(u), u))
    out: list[tuple[str, str, int]] = []
    index: dict[str, int] = {}
    consumed = 0  # 上一条链接（含其已配对提取码）在文本中的结束位置；前置提取码不得越过它
    for i, (start, end, u) in enumerate(spans):
        if u.startswith("//"):
            u = "https:" + u  # 协议相对链接 //pan.quark.cn/s/...
        elif not u.lower().startswith(("http://", "https://", "magnet:")):
            u = "https://" + u
        u, pwd = clean_share_url(u)
        link_end = end
        if not pwd:
            pre = _PWD_PRE_RE.search(t[max(consumed, start - PWD_WINDOW):start])
            if pre:
                pwd = pre.group(1)
        if not pwd:
            nxt = spans[i + 1][0] if i + 1 < len(spans) else None
            stop = nxt if nxt is not None else len(t)
            pm = _PWD_RE.search(t[end:min(stop, end + PWD_WINDOW)])
            # 码后紧跟冒号再接下一条链接 → 那是下一条的前置码，不归本条（避免把下一条的码错配给本条）
            if pm and not (nxt is not None and _PWD_PRE_GAP_RE.fullmatch(t[end + pm.end():nxt])):
                pwd = pm.group(1)
                end = end + pm.end()
        consumed = max(consumed, end)
        end = link_end
        key = _link_key(u)
        if key in index:
            j = index[key]
            if pwd and not out[j][1]:
                out[j] = (out[j][0], pwd, out[j][2])
            continue
        index[key] = len(out)
        out.append((u, pwd, start))
    return out


# ---- v1.7.7 --from_url：公开页面直链提取 ----
PAGE_MAX_URLS = 5
PAGE_MAX_LINKS = 100
PAGE_DELAY = (0.5, 1.5)
PAGE_SITES = (
    ("bilibili.com", "bilibili"), ("b23.tv", "bilibili"), ("tieba.baidu.com", "tieba"),
    ("zhihu.com", "zhihu"), ("douban.com", "douban"),
)
_WALL_TITLE_RE = re.compile(r"<title[^>]*>[^<]{0,80}?(登录|登陆|sign ?in|log ?in|禁止访问|access denied|forbidden)", re.I)
_WALL_TEXT = ("登录后查看", "登录后可见", "请先登录", "登录查看完整", "回复后可见")
SUGGEST_SITES = ("bilibili.com/opus", "bilibili.com/read", "tieba.baidu.com", "zhihu.com", "douban.com/group")
SUGGEST_TERMS = ("夸克 网盘", "pan.quark.cn")


def page_site(url: str) -> tuple[str, str]:
    host = (urllib.parse.urlsplit(url).netloc or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    for suffix, name in PAGE_SITES:
        if host == suffix or host.endswith("." + suffix):
            return name, "page:" + name
    return host or "unknown", "page:" + (host or "unknown")


def unescape_page(src: str) -> str:
    """页面源码解码：JSON 转义（\\u002F、\\/）、HTML 实体、跳转包装里 URL 编码的目标链接。"""
    t = src or ""
    t = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), t)
    if re.search("[\ud800-\udfff]", t):  # \uD83D\uDE00 这类代理对合并成真字符，孤立代理替换掉
        t = t.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    t = t.replace("\\/", "/")
    t = html_lib.unescape(t)
    t = re.sub(r"https?%3A(?:%2F|/){2}[^\s\"'<>]+",
               lambda m: urllib.parse.unquote(m.group(0)), t, flags=re.I)
    return t


def page_title(src: str) -> str:
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', src or "", re.I)
    if not m:
        m = re.search(r"<title[^>]*>(.*?)</title>", src or "", re.I | re.S)
    return re.sub(r"\s+", " ", html_lib.unescape(m.group(1))).strip()[:80] if m else ""


def page_wall(src: str) -> str:
    """登录墙/验证码判定（仅在页面未提取到链接时用于停站）。"""
    if looks_like_captcha(src):
        return "验证码/人机验证页"
    if _WALL_TITLE_RE.search(src or ""):
        return "登录墙/访问受限页"
    if any(w in (src or "") for w in _WALL_TEXT):
        return "正文需登录/回复后可见"
    return ""


def extract_page_links(src: str) -> list[tuple[str, str, str]]:
    """页面 → [(url, pwd, context)]：先在去标签正文里配对提取码，再补源码属性（href/JSON）里的链接。"""
    dec = unescape_page(src)
    body_hw = to_halfwidth(re.sub(r"<[^>]+>", " ", dec))
    out: list[tuple[str, str, str]] = []
    index: dict[str, int] = {}
    for is_body, part in ((True, body_hw), (False, to_halfwidth(dec))):
        for u, pwd, pos in _scan_links(part):
            key = _link_key(u)
            if key in index:
                j = index[key]
                if pwd and not out[j][1]:
                    out[j] = (out[j][0], pwd, out[j][2])
                continue
            # 上下文按正文中的**实际位置**取（旧实现按 URL 前缀 find，磁力/share/init 链会全部拿到第一条的上下文）
            ctx = _link_context(body_hw, pos) if is_body else ""
            index[key] = len(out)
            out.append((u, pwd, ctx))
    return out


def _link_context(body: str, pos: int) -> str:
    if pos <= 0:
        return ""
    ctx = re.sub(r"\s+", " ", body[max(0, pos - 80):pos])
    ctx = SHARE_LINK_RE.split(ctx)[-1]  # 不跨过上一条链接
    ctx = _PWD_RE.sub(" ", ctx)  # 去掉上一条链接的提取码
    ctx = re.split(r"[\"'{}\[\]<>=]", ctx)[-1]  # 去掉脚本/JSON 残片
    ctx = re.sub(r"(?:https?:)?/*$", "", ctx.strip()).strip()[-40:].strip(" \"'{}[]:,;")
    if re.fullmatch(r"[\w.:/?&#%\-]*", ctx, re.A):
        return ""  # 只剩标识符/URL 残片，不算正文
    return ctx


def page_cloud(u: str) -> str:
    c = cloud_from_url(u)
    if c == "others":
        if re.search(r"lanzn\.com", u, re.I):
            return "lanzou"
        if re.search(r"123pan\.cn", u, re.I):
            return "123"
    return c


def collect_pages(args, urls, cloud_types, include, exclude):
    """串行抓取公开页面（同站 0.5–1.5s 随机间隔；撞登录墙/验证码即停该站），返回 (items, pages, errors, totals)。"""
    errors, rows, pages, totals = [], [], [], {}
    urls = list(dict.fromkeys(u.strip() for u in urls if u.strip()))  # 去重保序
    if len(urls) > PAGE_MAX_URLS:
        errors.append("from_url: 单次最多 %d 个页面，已忽略其余 %d 个" % (PAGE_MAX_URLS, len(urls) - PAGE_MAX_URLS))
        urls = urls[:PAGE_MAX_URLS]
    visited, stopped = set(), {}
    for u in urls:
        if not re.match(r"https?://", u, re.I):
            u = "https://" + u
        site, label = page_site(u)
        info = {"url": u, "site": site, "source": label, "status": "", "links": 0, "title": ""}
        pages.append(info)
        if site in stopped:
            info["status"] = "skipped"
            info["error"] = "同站已撞%s，本次停止该站" % stopped[site]
            errors.append("%s: %s 跳过（%s）" % (label, u, info["error"]))
            continue
        if site in visited:
            time.sleep(random.uniform(*PAGE_DELAY))
        visited.add(site)
        try:
            src = http_html(u, timeout=25)
        except RiskControlError as e:
            stopped[site] = "风控/登录墙"
            info["status"] = "blocked"
            info["error"] = str(e)
            errors.append("%s: %s %s（风控/登录墙，已停止该站）" % (label, u, e))
            continue
        except Exception as e:
            info["status"] = "error"
            info["error"] = str(e)
            errors.append("%s: %s %s" % (label, u, e))
            continue
        links = extract_page_links(src)[:PAGE_MAX_LINKS]
        info["title"] = page_title(src)
        info["links"] = len(links)
        info["bytes"] = len(src)
        if not links:
            wall = page_wall(src)
            if wall:
                stopped[site] = wall
                info["status"] = "blocked"
                info["error"] = wall
                errors.append("%s: %s 未提取到链接（疑似%s，已停止该站）" % (label, u, wall))
            else:
                info["status"] = "empty"
                text_len = len(re.sub(r"\s+", "", re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", src, flags=re.S)))
                if text_len < 300 and len(src) < 15000:
                    info["hint"] = "前端渲染空壳（正文由 JS 加载），匿名 HTML 拿不到正文"
            continue
        info["status"] = "ok"
        totals[label] = totals.get(label, 0) + len(links)
        for lu, pwd, ctx in links:
            extra = {"detail": u}
            if ctx:
                extra["context"] = ctx
            rows.append(item(page_cloud(lu), lu, pwd, info["title"] or u, label, "", extra=extra))
    ns = argparse.Namespace(**vars(args))
    ns.kw, ns.limit = "", 0  # 页面链接：不按关键词排序/过滤、不做每盘条数截断（保留页面顺序）
    return finalize_items(rows, ns, cloud_types, include, exclude), pages, errors, totals


def suggest_queries(kw: str) -> list[str]:
    title = (kw or "").strip() or "<片名>"
    return ["site:%s %s %s" % (site, title, term) for site in SUGGEST_SITES for term in SUGGEST_TERMS]


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
    t0 = time.monotonic()
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
        elapsed = time.monotonic() - t0
        budget_left = min(pansou_timeout, PANSOU_TOTAL_TIMEOUT - elapsed)
        if budget_left <= 3.0:
            raise TimeoutError("pansou 总耗时超限 (50s)")
        try:
            if method == "GET":
                data = unwrap_pansou(http_json(url, timeout=budget_left))
            else:
                data = unwrap_pansou(
                    http_json(base.rstrip("/") + "/api/search", method="POST", body=body, timeout=budget_left)
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
        time.sleep(random.uniform(*DETAIL_DELAY))  # 同站详情：小随机间隔（v1.7.7）
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
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:  # 同站并发上限 2（v1.7.7，原 4）
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
    time.sleep(random.uniform(*DETAIL_DELAY))  # 同站详情：小随机间隔（v1.7.7）
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
        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:  # 同站并发上限 2（v1.7.7，原 4）
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
    # 1. 一级：pass_filters 初筛 + norm_url 精确去重
    raw_seen = set()
    raw_filtered = []
    for rec in items:
        if not pass_filters(rec, include, exclude, cloud_types):
            continue
        key = norm_url(rec.get("url") or "")
        if not key or key in raw_seen:
            continue
        raw_seen.add(key)
        raw_filtered.append(rec)

    # 2. 二级：(cloud, norm_title) 归一化标题分组聚合
    grouped = {}
    for rec in raw_filtered:
        c = rec.get("cloud") or "others"
        nt = norm_title(rec.get("note") or "")
        g_key = (c, nt) if nt else (c, norm_url(rec.get("url") or ""))
        grouped.setdefault(g_key, []).append(rec)

    # 3. 组内优选主条目：kw_hits 匹配度高者优先 → 包含日期更新者优先 → ENGINE_RANK 权重靠前优先
    #    主条目注入 dups: [{"url": u, "note": n, "source": s}, ...] 数组
    kw = getattr(args, "kw", "") or ""
    primary_items = []
    groups_list = []
    for g_key, grp in grouped.items():
        def _item_sort_key(r):
            hits = kw_hits(r, kw)
            dt = r.get("datetime") or ""
            dt_num = int(re.sub(r"\D", "", dt[:10])) if re.sub(r"\D", "", dt[:10]) else 0
            src = (r.get("source") or "").split(":")[0]
            rank = ENGINE_RANK.get(src, 5)
            return (-hits, -dt_num, rank)

        grp.sort(key=_item_sort_key)
        primary = grp[0]
        dups = [{"url": r["url"], "note": r.get("note") or "", "source": r.get("source") or ""} for r in grp[1:]]
        if dups:
            primary["dups"] = dups
        primary_items.append(primary)
        groups_list.append((primary, grp))

    # 4. 全局排序：日期新鲜度、关键词命中率与来源等级、失效链接沉底
    primary_items.sort(key=lambda r: r.get("datetime") or "", reverse=True)
    primary_items.sort(key=lambda r: (-kw_hits(r, kw), ENGINE_RANK.get((r.get("source") or "").split(":")[0], 5)))
    dead = load_dead_links()
    if dead:
        primary_items.sort(key=lambda r: norm_url(r["url"]) in dead)

    # 5. 夸克内容智能按需抽查 (--peek [smart|on|off])
    peek_mode = getattr(args, "peek", "smart")
    if should_peek(peek_mode, exclude):
        quark_targets = [r for r in primary_items if r.get("cloud") == "quark"][:5]
        if quark_targets:
            def _do_peek(rec):
                res = peek_quark(rec.get("url") or "", rec.get("password") or "")
                return rec, res

            with ThreadPoolExecutor(max_workers=min(5, len(quark_targets))) as pool:
                for fut in as_completed([pool.submit(_do_peek, r) for r in quark_targets]):
                    try:
                        rec, res = fut.result()
                        if res:
                            if res.get("tag"):
                                rec["content_tag"] = res["tag"]
                            if res.get("text"):
                                rec["content_peek"] = res["text"]
                    except Exception:
                        pass

        # 抽查后重新联动 pass_filters：若主条目被排除（如命中小说/引流），整组过滤
        surviving_primaries = []
        for rec in primary_items:
            if pass_filters(rec, include, exclude, cloud_types):
                surviving_primaries.append(rec)
        primary_items = surviving_primaries

    # 内容权重参与排序：纯小说/广告引流资源在常规影视搜索中沉底
    primary_items.sort(key=lambda r: 1 if r.get("content_tag") in ("[内容:小说]", "[内容:引流]") else 0)

    # 6. 处理 --all-dups：若开启则展开全部同名同质副本集合；否则保留折叠主条目
    if getattr(args, "all_dups", False):
        surviving_primary_urls = {norm_url(p["url"]) for p in primary_items}
        expanded = []
        for primary, grp in groups_list:
            if norm_url(primary["url"]) in surviving_primary_urls:
                expanded.extend(grp)
        # 保持排序
        expanded.sort(key=lambda r: r.get("datetime") or "", reverse=True)
        expanded.sort(key=lambda r: (-kw_hits(r, kw), ENGINE_RANK.get((r.get("source") or "").split(":")[0], 5)))
        if dead:
            expanded.sort(key=lambda r: norm_url(r["url"]) in dead)
        expanded.sort(key=lambda r: 1 if r.get("content_tag") in ("[内容:小说]", "[内容:引流]") else 0)
        final_list = expanded
    else:
        final_list = primary_items

    # 7. 限制每网盘展示条数
    if args.limit > 0:
        counts = {}
        trimmed = []
        for rec in final_list:
            c = rec["cloud"]
            counts[c] = counts.get(c, 0) + 1
            if counts[c] <= args.limit:
                trimmed.append(rec)
        final_list = trimmed

    return final_list


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
    peek_blob = " ".join(filter(None, [rec.get("content_tag"), rec.get("content_peek")]))
    blob = " ".join(filter(None, [rec.get("note"), rec.get("url"), peek_blob])).lower()
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
            if rec.get("context"):  # 仅 --from_url 页面链接有：链接前的正文，便于区分同页多条资源
                lines.append("     上下文: %s" % rec["context"])
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
            if rec.get("content_tag"):
                meta.append(rec["content_tag"])
            if meta:
                lines.append("     来源: " + " | ".join(meta))
            if rec.get("dups"):
                lines.append("     ↳ 另有 %d 个同资源副本链接（已折叠，--all-dups 展开）" % len(rec["dups"]))
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
        if not isinstance(st, dict):
            return {}
        now = datetime.now()
        changed = False
        for name in list(st.keys()):
            e = st[name]
            if not isinstance(e, dict):
                continue
            fails = int(e.get("fails") or 0)
            is_risk = bool(e.get("risk"))
            if not is_risk and fails < ENGINE_FAIL_LIMIT:
                continue
            try:
                cd = int(e.get("cooldown_min") or (RISK_COOLDOWN_MIN if is_risk else ENGINE_COOLDOWN_MIN))
            except (TypeError, ValueError):
                cd = RISK_COOLDOWN_MIN if is_risk else ENGINE_COOLDOWN_MIN
            try:
                last = datetime.fromisoformat(e.get("last_fail") or "")
                if (now - last) >= timedelta(minutes=cd):
                    st.pop(name, None)
                    changed = True
            except ValueError:
                pass
        if changed:
            save_engine_state(st)
        return st
    except (OSError, ValueError):
        return {}


def save_engine_state(st: dict) -> None:
    """先写临时文件再 os.replace（并发进程读到的要么是旧文件要么是新文件，不会读到半截 JSON）；
    替换失败（如 Windows 上文件正被占用）时回退为直接覆盖写。格式与 1.7.6 相同（多出的 risk/cooldown_min 键 1.7.6 会忽略）。"""
    try:
        d = os.path.dirname(ENGINE_STATE_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        data = json.dumps(st, ensure_ascii=False, indent=1)
    except (OSError, TypeError, ValueError):
        return
    tmp = "%s.%d.tmp" % (ENGINE_STATE_PATH, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, ENGINE_STATE_PATH)
        return
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
    try:
        with open(ENGINE_STATE_PATH, "w", encoding="utf-8") as f:
            f.write(data)
    except OSError:
        pass


def engine_blocked(name: str, st: dict) -> bool:
    """连续 ENGINE_FAIL_LIMIT 次失败后冷却 ENGINE_COOLDOWN_MIN 分钟（PanSeek 插件熔断的脚本化移植）。
    v1.7.7：强风控失败（risk=True：429/412/验证码页）一次即冷却 cooldown_min 分钟（默认 RISK_COOLDOWN_MIN）。
    v1.8.0：冷却期满后就地归零并持久化写回（抹平存量脏数据）。"""
    e = st.get(name) or {}
    if not e:
        return False
    is_risk = bool(e.get("risk"))
    if not is_risk and int(e.get("fails") or 0) < ENGINE_FAIL_LIMIT:
        return False
    if is_risk:
        try:
            cd = int(e.get("cooldown_min") or RISK_COOLDOWN_MIN)
        except (TypeError, ValueError):
            cd = RISK_COOLDOWN_MIN
    else:
        cd = ENGINE_COOLDOWN_MIN
    try:
        last = datetime.fromisoformat(e.get("last_fail") or "")
    except ValueError:
        return False
    if (datetime.now() - last) >= timedelta(minutes=cd):
        st.pop(name, None)
        save_engine_state(st)
        return False
    return True


_RISK_MSG_STRONG_RE = re.compile(
    r"HTTP (?:412|429)\b|HTTP Error (?:412|429)\b|验证码|人机验证|安全验证|captcha|风控|too many requests|请求过于频繁|rate.?limit", re.I)
_RISK_MSG_WEAK_RE = re.compile(r"HTTP (?:Error )?403\b", re.I)


def risk_info(exc) -> tuple[int, int]:
    """把引擎异常归类：(风控等级, Retry-After 秒)。等级 2=强风控（429/412/验证码，一次即熔断）、
    1=弱风控（403，按普通失败计数但本轮不再重试）、0=普通失败。
    先看异常链里的 RiskControlError，再按消息兜底匹配（ataw/盘小子会把子请求错误拼进消息）。"""
    e, depth = exc, 0
    while e is not None and depth < 6:
        if isinstance(e, RiskControlError):
            return (2 if e.strong else 1), e.retry_after
        e = e.__cause__ or e.__context__
        depth += 1
    msg = str(exc or "")
    if _RISK_MSG_STRONG_RE.search(msg):
        return 2, 0
    if _RISK_MSG_WEAK_RE.search(msg):
        return 1, 0
    return 0, 0


def risk_cooldown_min(retry_after: int) -> int:
    ra = min(max(0, int(retry_after or 0)), RISK_COOLDOWN_MAX_MIN * 60)
    return min(RISK_COOLDOWN_MAX_MIN, max(RISK_COOLDOWN_MIN, int(math.ceil(ra / 60.0))))


def mark_engine_failure(st: dict, name: str, level: int = 0, retry_after: int = 0) -> None:
    """失败计数回写（就地修改 st）。level=2 强风控：立即熔断（fails 拉到阈值以保证 1.7.6 读同一文件也会熔断）。"""
    e = st.get(name) or {}
    e["fails"] = int(e.get("fails") or 0) + 1
    e["last_fail"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    if level >= 2:
        e["fails"] = max(e["fails"], ENGINE_FAIL_LIMIT)
        e["risk"] = True
        e["cooldown_min"] = risk_cooldown_min(retry_after)
    else:
        e.pop("risk", None)
        e.pop("cooldown_min", None)
    st[name] = e


def mark_engine_success(st: dict, name: str) -> None:
    """引擎成功执行显式归零：彻底移除失败记录并重置额度。"""
    if name in st:
        st.pop(name, None)


def risk_blocked(name: str, st: dict) -> bool:
    return bool((st.get(name) or {}).get("risk")) and engine_blocked(name, st)


def load_dead_links() -> set:
    """已确认失效的链接集合（读 check_links 四级状态机文件）。
    借鉴 quark-auto-save「记录失效分享并跳过」：只降权不删除，仍会展示。"""
    try:
        with open(LINK_STATE_PATH, encoding="utf-8-sig") as f:
            d = json.load(f)
        return {norm_url(u) for u, r in d.items() if isinstance(r, dict) and r.get("state") == "bad"}
    except (OSError, ValueError):
        return set()


def _search_once(args, engines, cloud_types, include, exclude, kw=None, elapsed=None, risky_out=None):
    if kw and kw != args.kw:
        ns = argparse.Namespace(**vars(args))
        ns.kw = kw
        args = ns
    jobs = build_jobs(args, engines, cloud_types, include, exclude)
    by_name = {}
    errors = []
    totals = {}
    risky = {}
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
                errors.append(clip_err("%s: %s" % (name, e)))
                level, retry_after = risk_info(e)
                if level:
                    risky[name] = (level, retry_after)
            if elapsed is not None:
                elapsed[name] = round(time.monotonic() - t_start, 1)

    # 熔断状态回写：成功清零，失败累计（借鉴 PanSeek 插件熔断）
    st = load_engine_state()
    changed = False
    for name in jobs:
        if name in by_name:
            if name in st:
                mark_engine_success(st, name)
                changed = True
        elif any(x.startswith(name + ":") for x in errors):
            level, retry_after = risky.get(name, (0, 0))
            mark_engine_failure(st, name, level, retry_after)  # v1.7.7：强风控一次即熔断
            changed = True
    if changed or st:
        save_engine_state(st)
    if risky_out is not None:
        risky_out.update(risky)

    merged = []
    for name in ENGINE_ORDER:
        merged.extend(by_name.get(name) or [])

    items = finalize_items(merged, args, cloud_types, include, exclude)
    return items, totals, errors


def run_suggest(args) -> int:
    qs = suggest_queries(args.kw or "")
    if args.json:
        print(json.dumps({"keyword": args.kw or "", "queries": qs}, ensure_ascii=False, indent=2))
    else:
        print("宿主 WebSearch 建议查询（找到公开页面后用 --from_url 提取直链，单次 ≤%d 个）：" % PAGE_MAX_URLS)
        for q in qs:
            print("  " + q)
    return 0


def print_pages(pages: list) -> None:
    for pg in pages:
        bits = "%s %s：%s，%s 条" % (pg["source"], pg["url"], pg.get("status") or "?", pg.get("links") or 0)
        if pg.get("error"):
            bits += "（%s）" % pg["error"]
        print("页面提取: " + bits)


def run_pages(args, page_urls) -> int:
    """仅页面提取模式（--from_url 且未给 --kw）：不跑任何搜索引擎。"""
    cloud_types = split_csv(args.cloud_types)
    include = split_csv(args.include)
    exclude = split_csv(args.exclude)
    items, pages, errors, totals = collect_pages(args, page_urls, cloud_types, include, exclude)
    result = {
        "keyword": "",
        "total": len(items),
        "totals": totals,
        "errors": errors,
        "results": items,
        "pages": pages,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_pages(pages)
        print(format_text("（页面提取，%d 个页面）" % len(pages), items, errors, totals))
    return 0 if items or not errors else 1


def run(args):
    if getattr(args, "suggest_queries", False):
        return run_suggest(args)
    page_urls = split_csv(getattr(args, "from_url", None))
    if page_urls and not args.kw:
        return run_pages(args, page_urls)
    engines = [e.strip() for e in (args.engine or "pansou,haisou,yunso,ataw").split(",") if e.strip()]
    if "all" in engines:
        # all = 默认四源（含 ataw），且保留同批次显式指定的其它引擎（如 all,panxiaozi）
        engines = ["pansou", "haisou", "yunso", "ataw"] + [e for e in engines if e not in ("all", "pansou", "haisou", "yunso", "ataw")]

    # WP2: 引擎软校验机制（友好拦截未知引擎与废弃 ghspider）
    valid_engines = []
    for e in engines:
        if e == "ghspider":
            sys.stderr.write('⚠️ 引擎 "ghspider" 已于 v1.7.5 废弃移除，TG 频道资源检索请使用: python scripts/search.py --kw "..." --src all\n')
            continue
        if e not in SUPPORTED_ENGINES:
            sys.stderr.write('⚠️ 未知引擎 "%s"，已跳过（可用: pansou,haisou,yunso,ataw,panxiaozi,movie,local）\n' % e)
            continue
        if e not in valid_engines:
            valid_engines.append(e)
    engines = valid_engines

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
    risky_now = {}
    items, totals, errors = _search_once(args, engines, cloud_types, include, exclude, elapsed=elapsed, risky_out=risky_now)

    # 关键词变体兜底（借鉴 PanHub searchKeyword 思路）：0结果时用变体重试一轮
    variant_used = ""
    if not items and not getattr(args, "no_variants", False):
        # v1.7.7：本轮刚撞风控（429/412/验证码/403）的引擎不参与变体重试（避免立即再撞）；普通失败行为不变
        v_engines = [e for e in engines if e not in risky_now]
        for v in kw_variants(args.kw) if v_engines else []:
            v_items, v_totals, v_errors = _search_once(args, v_engines, cloud_types, include, exclude, kw=v, elapsed=elapsed)
            if v_items:
                variant_used = v
                items = v_items
                totals = v_totals
                errors = errors + [clip_err("变体「%s」: %s" % (v, e)) for e in v_errors]
                break

    ataw_fallback = False
    if should_ataw_fallback(engines, items) and risk_blocked("ataw", load_engine_state()):
        errors.append("ataw(fallback): 风控冷却中，已跳过")
    elif should_ataw_fallback(engines, items):
        try:
            fb_rows, fb_total = search_ataw(args.kw, args.limit)
            if fb_rows:
                ataw_fallback = True
                totals["ataw"] = fb_total
                items = finalize_items(items + fb_rows, args, cloud_types, include, exclude)
        except Exception as e:
            errors.append(clip_err("ataw(fallback): %s" % e))
            level, retry_after = risk_info(e)
            if level >= 2:  # 兜底撞强风控也记入熔断，下次兜底直接跳过
                st_fb = load_engine_state()
                mark_engine_failure(st_fb, "ataw", level, retry_after)
                save_engine_state(st_fb)

    pages = None
    if page_urls:
        p_items, pages, p_errors, p_totals = collect_pages(args, page_urls, cloud_types, include, exclude)
        seen_urls = {norm_url(r["url"]) for r in items}
        items = items + [r for r in p_items if norm_url(r["url"]) not in seen_urls]
        errors = errors + p_errors
        totals.update(p_totals)

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
    if pages is not None:
        result["pages"] = pages
        result["page_note"] = "页面链接未按 --kw 过滤（--kw 只作用于引擎搜索），排在引擎结果之后"
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if skipped:
            print("熔断跳过: %s（--fresh 强制重试）" % "、".join(
                e + ("(风控)" if (st.get(e) or {}).get("risk") else "") for e in skipped))
        if elapsed:
            slow = sorted(elapsed.items(), key=lambda x: -x[1])
            print("各源耗时: " + "；".join("%s %.1fs" % (k, v) for k, v in slow))
        if pages is not None:
            print_pages(pages)
        print(format_text(args.kw, items, errors, totals))
        if pages is not None:
            print("（页面链接未按关键词过滤，排在引擎结果之后）")
        if variant_used:
            print("（0 结果已自动用变体「%s」重试）" % variant_used)
        if ataw_fallback:
            print("（默认源无可用直链，已自动尝试 TA搜 ataw 兜底）")
    return 0 if items or not errors else 1


def main():
    p = argparse.ArgumentParser(description="多源网盘资源搜索")
    p.add_argument("--kw", help="搜索关键词（必填；仅 --from_url / --suggest_queries 时可省略）")
    p.add_argument("--cloud_types", help="网盘类型，逗号分隔，如 quark,aliyun,baidu")
    p.add_argument("--include", help="结果须含这些词，逗号分隔")
    p.add_argument("--exclude", help="排除这些词，逗号分隔")
    p.add_argument("--src", default="plugin", choices=["all", "tg", "plugin"], help="盘搜数据源，默认 plugin（更快更稳）")
    p.add_argument("--engine", default="pansou,haisou,yunso,ataw", help="pansou,haisou,yunso,ataw,panxiaozi,movie,local；all=默认四源；若遇废弃引擎（如 ghspider）自动友好提示")
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
    p.add_argument("--from_url", help="公开页面 URL，逗号分隔，单次最多 %d 个：抓页面提取网盘直链（无 --kw 时只做页面提取）" % PAGE_MAX_URLS)
    p.add_argument("--suggest_queries", action="store_true", help="打印供宿主 WebSearch 用的 site: 查询建议（配合 --kw），不联网")
    p.add_argument("--all-dups", action="store_true", help="展开全部同名同质资源副本（默认折叠同名资源并注记副本数）")
    p.add_argument("--peek", choices=["smart", "on", "off"], default="smart", help="夸克网盘内容智能抽查模式：smart（默认，命中小说/电子书等排除词时自动触发）、on（强制抽查 Top 5）、off（关闭抽查，秒级返回）")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    if args.kw is not None and not args.kw.strip():
        args.kw = None  # 空白关键词视同未提供
    if not args.kw and not split_csv(args.from_url) and not args.suggest_queries:
        p.error("必须提供 --kw（或使用 --from_url / --suggest_queries）")
    sys.exit(run(args))


if __name__ == "__main__":
    main()
