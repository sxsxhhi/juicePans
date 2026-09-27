# 数据源

## 脚本引擎

**本沙箱实测**列基于 WorkBuddy / Cloud Agent 环境实跑结果。

| 引擎 | 默认？ | 地址 | 说明 | 本沙箱实测 |
|---|---|---|---|---|
| `pansou` | 是 | `https://so.252035.xyz/api/search` | 公开盘搜；公开站先 GET（有 include/exclude 时先 POST）。TG：`--src all` | ✅ GET 可用 |
| `haisou` | 是 | `https://haisou.cc/api/v2/shares/search` | 不要传 `platforms:["all"]`（会 422） | ⚠️ 可用但常 429 |
| `yunso` | 是 | `https://www.yunso.net/api/opensearch.php` | `wd` + `mode`（90001 智能 / 90002 精准） | ✅ 可用 |
| `ataw` | **是（默认，v1.7.6）** | `https://so.ataw.top` | TA搜：SSR `/?q=&b=`（`b`=quark/ali/sharepan）→ `GET /api/v1/public/resources/{id}` 取直链；来源标签如 `ataw:quark`；单 biz 软失败（v1.7.5 整合） | ✅ 可用 |
| `panxiaozi` | 否 | `https://pan.xiaozi.cc/resource?q=<kw>` | 盘小子 SSR + 详情直链 | ✅ 可用 |
| `movie` | 否 | `https://meng-ge.top/api/movieData/getMoviesByType` | 影视库；需 `--engine movie` | ❌ 502（部分环境被拦） |

`--engine all` = **`pansou,haisou,yunso,ataw` 默认四源**（不含 `movie` / `panxiaozi`）。

用户显式排除 `ataw` 且默认三源无可用分享直链时，脚本会在 Agent WebSearch 之前**自动**补搜 `ataw`（JSON 可含 `ataw_fallback`）。默认调用下 ataw 已在第一轮运行。

### TG 频道（公开盘搜 `--src all`）

技能路径推荐用公开盘搜扩大源，**不要**把局域网自建 PanSou 当默认技能路径。

`--src all` 时盘搜会聚合 TG 插件，常见频道包括（上游可能变动）：

- `tgsearchers7`
- `Quark_Movies`
- `yunpanquark`
- `QuarkFree`
- `guoman4K`
- `yunpanx`

## 自建 PanSou（高级 / 备查，非技能默认路径）

上游：<https://github.com/fish2018/pansou>

```text
docker run -d --name netdisk-search -p 8888:8888 --restart unless-stopped ghcr.io/fish2018/pansou:latest
```

仅当用户**自行部署**且环境变量指向实例时，脚本才可能通过 `local` 引擎探测（见 `scripts/deploy.sh`）。**不要在技能文档中推荐 `--engine local` 或家庭 LAN 盘搜作为常规用法。**

## GitHub 同类项目评估（节选）

| 仓库 | 结论 | 说明 |
|---|---|---|
| [towelong/panxiaozi](https://github.com/towelong/panxiaozi) | ✅ **已整合**（`panxiaozi`） | SSR + 详情直链 |
| so.ataw.top（TA搜） | ✅ **已整合**（`ataw`，v1.7.5） | 公开 SSR + REST 详情 API |
| [John-h-netdisk/netdisk-spider](https://github.com/John-h-netdisk/netdisk-spider) | ❌ **已移除**（原 `ghspider`） | v1.7.5 起改由公开盘搜 `--src all` 覆盖 TG；不再维护 GitHub 数据集引擎 |
| [fish2018/pansou](https://github.com/fish2018/pansou) | ✅ 公开实例 + 可选自建 | 默认 `pansou` 引擎上游 |
| 其余评估记录 | 见历史版本 | 未变结论的仓库仍按原表「不整合」 |

## 不要做的

- 深度页面爬虫（cloudscraper / 并发抓站）
- 把下表网页站当脚本引擎
- 假地址、硬依赖 `requests`、写死内网 IP / 代理凭据
- 把 Hermes / OpenClaw 安装说明当必装步骤

## 网页搜索站测评（2026-09-15，Cursor 与 WorkBuddy 独立复测结论一致）

判定：普通 HTTP 拿到网盘直链才进脚本。SPA / 校验 / 中间页 / 证书挂了就不爬。下表 15 站即用户整理提交的一批，
**两版均判定不可直接接入**（普通 HTTP 拿不到直链），按用户要求本版暂不纳入。

| 站 | 结论 | 原因 |
|---|---|---|
| 混合盘 hunhepan.com | 不加 | `POST /open/search/disk` 有 JSON，但本沙箱握手失败、实测 0 条，精修时已移除 |
| 爱搜 esoua.com | 不加 | 前端壳，无直链 |
| 橘子盘搜 nmme.icu | 不加 | 跳 nmme.one 后 404 |
| 懒盘 lzpanx.com | 不加 | SSL 握手超时 |
| 盘搜 panso.pro | 不加 | 前端壳 |
| 帕卡 cuppaso.com | 不加 | 站内 `share/数字:token`，不是网盘 URL |
| 盘搜搜 / 小白盘 zhiso.cc | 不加 | **同一 URL**；JWT 跳转 |
| 搜网盘 zhongchuangwl.com | 不加 | `/tag/关键词/` 404 |
| 夸克探宝 quarkfinder.top | 不加 | 403 / Cloudflare |
| 夸克盘搜索 pansosuo.com | 不加 | 校验页 |
| 夸克搜 qkpanso.com | 不加 | 前端壳 |
| 毕方铺 iizhi.cn | 不加 | 证书过期 / 502 |
| 云盘吧 yunpan8.net | 不加 | 浏览器检查页 |
| 奇乐搜 qileso.com | 不加 | Cloudflare |
| 爱盘搜 aipanso.com | 不加 | Cloudflare + JS 加密 + `/s/` 中间页 + 同意声明 |
