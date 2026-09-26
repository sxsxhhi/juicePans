---
name: juicePans
version: 1.7.5
description: >-
  果汁搜盘（英文名 juicePans）：搜索公开网盘影视与资料分享链接（夸克、百度、阿里、迅雷、UC、115、天翼、移动、123、蓝奏、磁力等）。
  默认并行查询公开盘搜、海搜、小云搜索，去重后智能排序并按网盘分组；可选 TA搜（ataw）、盘小子、影视库。
  默认源失败或无可用直链时脚本自动 TA搜 兜底（JSON 可含 ataw_fallback），再交宿主 WebSearch 穷尽公开检索。
  内置引擎熔断、关键词变体兜底重试与 8 类网盘匿名链接核验。
  纯标准库、零依赖；上报前区分「已核验」与「公开检索（未核验）」，不编造链接/提取码/来源。
  只搜不转存：链接须先核验存活并列给用户，经用户批准并指明目标目录后才交夸克转存。
  Use when the user asks 搜网盘、找资源、找电影、找剧、找动漫、搜片、网盘链接、夸克资源、百度网盘、阿里云盘、
  海搜、盘搜、PanSou、小云搜索、yunso、TA搜、ataw、找下载、提取码, or gives a title and wants share links.
---

# 果汁搜盘 juicePans（网盘资源搜索）

本技能由 Cursor 整合版（1.1.0）与 WorkBuddy 整合版合并而来，并按多环境实测调校（沿革见 [整合说明.md](整合说明.md)）。
脚本相对本技能根目录，纯标准库，不要装 Docker / uv / requests。
Windows 控制台先设 UTF-8：`$env:PYTHONUTF8=1`；解释器优先 `python`，没有用 `python3`。

## 本沙箱实测可用性（重要，决定默认源）

| 引擎 | 本沙箱 | 说明 |
|---|---|---|
| `pansou` 公开盘搜 | ✅ GET 可用 | 公开站 `POST` 会被代理改坏/超时，脚本已**默认 GET 优先**；TG 频道走 `--src all`（见下） |
| `yunso` 小云搜索 | ✅ 可用 | JSON 接口 `opensearch.php`（`wd`+`mode`）；直链在 `Data[].Scrurl` |
| `haisou` 海搜 | ⚠️ 可用但常 429 | 共享出口 IP 限流；单源失败不中断，自动降级 |
| `ataw` TA搜 | ✅ 可用（v1.7.5） | [so.ataw.top](https://so.ataw.top) SSR 搜索 + 公开详情 API；夸克/阿里/sharepan；**非默认**，但默认三源无果时脚本**自动兜底** |
| `panxiaozi` 盘小子 | ✅ 可用 | SSR 搜索页 + 详情页直链；来源 [towelong/panxiaozi](https://github.com/towelong/panxiaozi) |
| `movie` 影视库 | ❌ 502 域名被拦 | `meng-ge.top` 在本沙箱被拦；仅 `--engine movie` 时尝试 |

> 另有 15 个网页搜盘站实测均为 SPA 壳 / Cloudflare / 404，普通 HTTP 拿不到直链，已列黑名单暂不接入——详见 [sources.md](references/sources.md)。

### TG 频道（公开盘搜，非自建）

需要 TG 聚合结果时，用**公开**盘搜并扩大数据源，不要走局域网自建 PanSou：

```text
python scripts/search.py --kw "片名" --src all
```

公开盘搜 `--src all` 会较慢，但可覆盖 TG 插件；常见频道包括：`tgsearchers7`、`Quark_Movies`、`yunpanquark`、`QuarkFree`、`guoman4K`、`yunpanx`（以盘搜上游为准，频道可能变动）。

## 何时用 / 何时不用

**用**：用户给片名或资料名，要公开分享链接。本技能只检索、展示，**默认不转存**；必须先核验存活并列给用户，**等用户批准并指明目标目录后**才走夸克转存技能（见下文「转存铁律」）。

**不用、直接拒绝**（不给替代检索）：盗版软件/注册机/激活工具、恶意程序、涉黄涉暴赌毒、隐私数据包。不编造链接、提取码、来源或时间。

只整理第三方公开结果，不托管文件。链接可能失效，下载前自行判断安全性。

## 流程

1. 抽出关键词（片名优先中文；季/画质/网盘有就带上）。模糊先问清。用户没想好找什么时，可用 `--engine movie` 列影视库热门/4K 榜单当发现入口（借鉴 PanHub 豆瓣榜单思路）。
2. 在本技能根目录跑搜索脚本。用户指定网盘则加 `--cloud_types`；要 4K、不要预告用 `--include` / `--exclude`。脚本内置三项增强（v1.5.0）：综合排序（关键词匹配度 > 来源等级 > 时间新鲜度）、引擎熔断（连续失败 2 次自动冷却 30 分钟，`--fresh` 强制全跑）、0 结果自动用关键词变体重试一轮。
3. 把 stdout 按网盘分组给用户。链接必须可点，禁止用代码块包 URL。同一 URL 只出现一次。同组相似候选多时，按「标题匹配 > 命名规范（集数/版本信息全）> 更新时间」把最佳匹配排前（借鉴 mediary-scout）。
4. **兜底顺序（v1.7.5）**：默认三源（`pansou,haisou,yunso`）都失败或**没有可用分享直链** → 脚本**自动**跑 TA搜（`ataw`，软失败不中断；`--json` 时可能含 `ataw_fallback: true`）→ 仍无果再进入 **v1.7.0 全网穷尽**：宿主 WebSearch 多轮不设上限、直抓公开页面提取直链、IMDb/百科别名交叉补搜、GitHub 聚合与 BT/磁力备选；硬约束不变（不爬 [sources.md](references/sources.md) 黑名单、不对单站高频连发、撞登录墙/验证码即止、不编造结果）。
5. 候选中出现「失效 / 疑似失效」时，自动补搜下一候选替换（自动换链，借鉴 cloud-auto-save-x），不把死链端给用户。
6. 无结果就如实说，建议换原名/简称，不要凑数。

> 频率纪律（借鉴 quark-auto-save / CASX 风控经验）：不对同一站点高频连发请求；多引擎并行限流已由脚本内置，人工兜底搜索同样保持克制。

## 命令

```text
python scripts/search.py --kw "星际穿越"
python scripts/search.py --kw "流浪地球2" --cloud_types quark,aliyun --include 4K --exclude 预告,CAM --limit 5
python scripts/search.py --kw "三体" --engine pansou,yunso
python scripts/search.py --kw "庆余年" --engine panxiaozi
python scripts/search.py --kw "庆余年" --engine ataw --json
python scripts/search.py --kw "片名" --src all
python scripts/search.py --kw "庆余年" --engine all,panxiaozi --json
```

| 参数 | 说明 |
|---|---|
| `--kw` | 必填。海搜还支持 `"精确短语"` 和 `-排除词` |
| `--cloud_types` | `quark,aliyun,baidu,...`，见 [cloud-types.md](references/cloud-types.md) |
| `--include` / `--exclude` | 逗号分隔；盘搜可走服务端，其它源本地再滤 |
| `--engine` | 默认 `pansou,haisou,yunso`。`all` = 这三源。`ataw` / `movie` / `panxiaozi` 需显式指定（`ataw` 不在 `all` 内；默认源无果时仍会自动 ataw 兜底） |
| `--yunso_mode` | `90001` 智能（默认），`90002` 精准 |
| `--src` | 仅盘搜：默认 `plugin`。`all` 含 TG，较慢 |
| `--scope` | 仅海搜：`title`（默认）或 `files` |
| `--min_size` / `--max_size` | 仅海搜，单位 GB |
| `--limit` | 每种网盘最多展示条数，默认 8 |
| `--page` / `--page_size` | 分页（默认 1 / 10），作用于海搜 / 小云 / 影视库（盘搜不支持分页） |
| `--refresh` | 盘搜绕过缓存 |
| `--pansou_timeout` | 盘搜超时秒数（默认 45）。公开盘搜是聚合源、天然偏慢（实测 40s+）；急用可调小如 `--pansou_timeout 15`，代价是聚合不全。文本输出含「各源耗时」可定位慢源 |
| `--fresh` | 忽略引擎熔断状态，强制全部引擎执行 |
| `--no-variants` | 禁用 0 结果时的关键词变体自动重试 |
| `--json` | 机器可读（含 `elapsed`、可选 `ataw_fallback`） |

## 链接核验（本版规则）

- **夸克链接**：优先用夸克 CLI 实测存活；`scripts/check_links.py` 在检测到 `QUARK_SKILL_DIR` + `NODE_BIN` 时自动走夸克 CLI。
- **其它公开链接（v1.6.0 起内置匿名核验，借鉴 fish2018/NetDiskLinkValidator）**：`check_links.py` 内置夸克（CLI 缺席时的兜底）/阿里/115/123/天翼/百度/蓝奏/UC 八类匿名检测端点，无需 cookie/token；不支持的类型（迅雷/移动/PikPak 等）仍标「未核验」，不假装核验。
- **四级状态机（v1.4.0 新增，借鉴 supansou/DuPanSou-Archive）**：`check_links.py` 把每次实测结果存本地 JSON（默认 `~/.pan_search/link_state.json`，可用 `--state` / `LINK_STATE_PATH` 改），跨次生效：
  - 状态四级：**有效** / **疑似失效**（首败）/ **确认失效**（连续 2 次失败才判死，防误杀）/ **未核验**
  - 复检策略：有效 72h 内不复检；疑似 30min 内不重复检；失效 12h 后自动复查是否恢复
  - **检测异常（CLI 报错 / 接口失败）不计入失败**——只有拿到明确「失效」判定才累计
  - 磁力 / 电驴链接不做有效性检测，**永不误伤**
  - `--force` 忽略缓存策略全部强制实测；`--no-state` 本次不读写状态
  - 汇报口径：缓存的「有效」要说明是几小时前实测的；「疑似失效」不得说成「已失效」
- **失效链降权（v1.6.0 新增，借鉴 quark-auto-save）**：`search.py` 读取状态机文件，「确认失效」的链接在搜索结果中自动沉底（仍展示、不删除）

```text
python scripts/check_links.py --url "https://pan.quark.cn/s/xxxx"
python scripts/check_links.py --file links.txt --json
python scripts/check_links.py --file links.txt --force   # 忽略缓存全量实测
```

**内容核查（夸克链推荐）**：`check_links.py` 只能证明「链接还活着」，证明不了里面有什么——夸克分享的根目录常是一个文件夹。
用夸克 CLI 翻子目录看真片/广告图（`--pdir-fid` 取上一层的 `files[].fid`）：

```text
cd <夸克网盘skill目录> && node scripts/quark-drive.cjs share-detail --url "<夸克链>" --pdir-fid <子目录fid>
```

注意：`check_links.py` 报的「N 个文件」是**根目录条目数**，根若是文件夹恒为 1，别当成内容数量。
分享里常夹带 `网盘搜索小程序….png`、`全网资源扫码自取.png`、`1080.mp4` 同级的广告图，转存时不要勾选。

数据源与网页站黑名单：[sources.md](references/sources.md)。盘搜接口细节：[pansou-api.md](references/pansou-api.md)。合规红线：[compliance.md](references/compliance.md)。

## 转存铁律（先核验 → 列给用户 → 批准后才转存）

**本技能只负责「搜到 + 核验 + 列出来」，转存是下一步，且必须先拿到用户批准。** 顺序写死，不可颠倒、不可跳过：

1. **先核验存活**：每条候选链接上报前都用 `check_links.py` 过一遍（夸克链自动走夸克 CLI 实测）。
   失效 / 已取消分享 / 被封禁的，**不得**混进待选清单——单独列为「已失效」并写明原因。
2. **列给用户看**：按网盘分组展示（标题 / 可点链接 / 提取码 / 来源 / 日期 / 核验状态），并说明每条里到底是什么（见上文「内容核查」，别只报链接是否活着）。
3. **等用户批准**：明确问「要哪几条 + 转到哪个目录」，**拿到明确指定后才动**。
4. **批准后才转存**：转存交 `quark-share-relay` + 夸克网盘官方 CLI 执行，本技能不自己转。

红线：

- ❌ 未经用户批准，**不得**转存、不得生成分享链接、不得移动/整理网盘任何文件。
- ❌ 不得把「未核验」说成「有效」；也不得把「核验失败」直接当成「链接失效」——如实标状态与原因。
- ❌ 用户没指定目标目录时，**不要**自己挑一个目录转（问清楚再动）。
- ⚠️ 核验通过 ≠ 内容对得上：分享里可能全是广告图或同名不同片，上报前按「内容核查」展开子目录确认。

## 展示

先结果后短说明。每条：标题、可点链接、提取码（没有就写无）、来源、日期（有则写）。海搜体积/文件数、影视库「热门」放备注。

```markdown
**【夸克网盘】**
1. 星际穿越 Interstellar (2014)
   链接：https://pan.quark.cn/s/xxxxx
   提取码：无
   来源：pansou:plugin:susu | 2023-09-10
```

表格也可以。链接列用 Markdown 可点击形式，不要用反引号包裹地址。

结尾：

> 链接来自公开检索，可能失效；请自行核对文件安全。涉及付费作品请走正版渠道。

## 环境（可选）

| 变量 | 作用 |
|---|---|
| `QUARK_SKILL_DIR` / `NODE_BIN` | 夸克 CLI 核验所需；指向夸克网盘 skill 目录与 node 可执行 |

未配置时不要跑 Docker、不要用占位地址。

## 故障（本沙箱实测）

| 现象 | 处理 |
|---|---|
| 某一源失败、其它有结果 | 交付有结果的，并注明失败源（脚本已内置，不会中断） |
| pansou `POST` 超时 / 400 | 脚本对公开站**默认 GET**；仍失败则 `--engine yunso,haisou` 或等 ataw 自动兜底 |
| 海搜 429 | 连续失败 2 次会熔断冷却 30 分钟自动恢复；急用 `--fresh` 或 `--engine pansou,yunso` |
| 影视库 502 | `meng-ge.top` 本沙箱被拦；仅 `--engine movie` 时试，失败即跳过 |
| ataw 单 biz 失败 | 其它 biz 仍返回；全失败时在 `errors` 里见 `ataw(fallback):` |
| 检测接口 404 / 无服务 | 内置 8 类匿名检测可直接用（夸克/阿里/115/123/天翼/百度/蓝奏/UC）；其余类型不编有效/失效 |
| 全部失败 | 换关键词（脚本已自动试变体 + ataw）；仍无则 WebSearch 穷尽兜底 |

## 检查清单

- [ ] 本目录 `scripts/search.py`；Windows 优先 `python`，`$env:PYTHONUTF8=1`
- [ ] 只搜不转存：未核验的不上报；已核验的也要**等用户批准 + 指明目录**后才转存（见「转存铁律」）
- [ ] 链接可点，无代码块包 URL，无编造
- [ ] 多源去重；失败源已说明；若 JSON 含 `ataw_fallback` 向用户说明已自动 TA搜 兜底
- [ ] 链接已尽量核验（夸克 CLI / 内置 8 类匿名检测）；不支持的类型标注「未核验」
- [ ] 违法检索已拒绝
