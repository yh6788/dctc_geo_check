# dctc_geo_check

**[English](#english)** · **[中文](#中文)**

Core algorithm for generative-engine (GEO) citation readiness audits · 网站 GEO 自动体检核心算法.

Live service / 线上服务: **[https://www.geohttp.com](https://www.geohttp.com)** (GEO Cool / GEO库)

---

# English

Automated GEO (Generative Engine Optimization) audit: check whether a public page is easy for AI crawlers to fetch, understand, and cite.

## Try it online (recommended)

This repository is the command-line core. The full hosted product is:

**[https://www.geohttp.com](https://www.geohttp.com)** (GEO Cool / GEO库)

Paste a public URL to get:

- A 0–100 score and band
- Five-axis breakdown, evidence, and fix order
- Multi-view reports (crawler probes, full catalog, observations, and more)

No install and no account required for a trial.

- English site: [https://www.geohttp.com/en](https://www.geohttp.com/en)
- Scoring method (zh): [https://www.geohttp.com/method](https://www.geohttp.com/method)
- Scoring method (en): [https://www.geohttp.com/en/method](https://www.geohttp.com/en/method)

`03-geo_check.py` in this repo is the same judging logic used by [geohttp.com](https://www.geohttp.com) in production (five-axis v11).

## Five-axis scoring (100 total)

The base score is weighted across five axes. Fetch failures are **unknown**: they are not scored as zero in the numerator; they only lower confidence. Micro-adjust and the P0 cap apply after the base score.

| Axis | Max | What we check |
|---|---:|---|
| Citation reach | 28 | Citation bots explicitly allowed, live HTTP 200, sitemap |
| Structured data | 18 | Valid JSON-LD, key types, FAQPage / QAPage |
| Citable content | 24 | Answer first, tables or authority links, time/price dates; bonus pool ≤5 |
| On-page entity | 18 | Organization, sameAs, @id/profile, visible author and FAQ |
| Technical rendering | 12 | No nested documents, lang, canonical, social tags, compression, HTTP/2 |

### 1. Citation reach · 28

| Item | Max | Notes |
|---|---:|---|
| Citation bots explicitly allowed | 12 | Core set: OAI-SearchBot, Claude-SearchBot, PerplexityBot, Googlebot, Applebot; full-site `Disallow: /` is P0 |
| AI crawlers reachable (live) | 10 | Re-request the submitted URL with representative UAs; citation/search bots must return 200 |
| Sitemap available and declared | 6 | Root sitemap opens, and robots has a `Sitemap:` line |

Blocking training bots (GPTBot, ClaudeBot, CCBot, etc.) is a site policy choice and is not scored as a failure.

### 2. Structured data · 18

| Item | Max | Notes |
|---|---:|---|
| JSON-LD present and valid | 8 | At least one parseable `application/ld+json` block; syntax errors fail the item |
| Key types | 5 | Organization / WebSite / Article / Product / BreadcrumbList / BlogPosting — four hits for full marks |
| FAQPage / QAPage | 5 | Add only when the page truly has Q&A; missing FAQ on a brand homepage is expected |

Entity points live on the **On-page entity** axis so they are not double-counted with types and Q&A.

### 3. Citable content · 24

| Item | Max | Notes |
|---|---:|---|
| Answer first or Q&A | 8 | A self-contained conclusion in main content, or FAQ / QA structure |
| Data table or authority links | 6 | Table with headers, or links to government / education / encyclopedia / vendor sites |
| Time and price dates | 5 | `time` with datetime; if prices appear, their date must be visible |
| Content bonus pool | ≤5 | Title length, meta description, heading order, question headings, favicon, lazy-load placement, script budget — 1 point each, pool capped at 5 |

### 4. On-page entity · 18

| Item | Max | Notes |
|---|---:|---|
| Organization declared | 4 | Organization in JSON-LD |
| sameAs authority anchors | 5 | At least two sameAs links (encyclopedia, official social, registry, or other official sites) |
| @id and name/url | 3 | Stable `@id` plus name and url |
| Visible author byline | 3 | Author visible on the page |
| Visible FAQ / Q&A | 3 | Visible Q&A block (complements structured FAQ types) |

### 5. Technical rendering · 12

| Item | Max | Notes |
|---|---:|---|
| No nested documents | 3 | Only one html / head / DOCTYPE set |
| Language declared | 2 | `html` has `lang` |
| canonical | 2 | Canonical pointing at this page |
| Open Graph / Twitter | 2 | Social tags meet the threshold |
| Compression | 2 | gzip or brotli |
| HTTP/2 | 1 | Protocol is HTTP/2 |

## Micro-adjust, cap, and bands

| Rule | Notes |
|---|---|
| Discovery files | Root llms.txt, ai.txt, or `/ai/*.json`, etc. — up to **+5** |
| Injection self-check | High-signal injection phrases, `data-ai-` attributes, zero-width characters — up to **−5** |
| Combined limit | Micro-adjust stays within **−5 ~ +5** |
| P0 cap | Any unfixed P0 caps the final score at **74** |
| Unknown | Timeout / connection failure → unknown; not zero in the numerator; confidence only |

Bands: 90+ excellent · 75–89 good · 50–74 pass · below 50 needs work.

## Multi-dimensional checks (beyond scored rows)

Besides five-axis scored items, the script also runs the checks below. Some enter the issue list or report views and **do not own a share of the base 100** (same as the site’s “Observations / All checks” views).

### Crawlers and platforms

- Citation, retrieval, training, and classic search-engine UA lists plus robots policy
- Multi-UA live reachability probes
- Platform readiness matrix (e.g. Google AI Overviews, ChatGPT Search, Perplexity, Claude, Copilot) — display only; not added again into the 100

### Page and content observations

- Primary h1, semantic tags, meta misplaced in body
- Security response headers
- Full update date, modern image formats, DOM size hints
- Title / description length, favicon, heading order, question-style headings
- Lazy-load placement, script budget
- llms.txt quality hints
- Training-bot policy, Crawl-delay (observe)
- Agent image dimensions (width/height hints)

### Discovery and safety

- AI discovery endpoints: llms.txt, `/.well-known/ai.txt`, `/ai/summary.json`, etc.
- GEO injection / hidden-text self-check

Issues are graded **P0 → P3**. P0 breaks the citation path (e.g. citation bots blocked site-wide, missing lang, missing/invalid JSON-LD, nested documents).

Full scoring copy on the site: [Scoring (en)](https://www.geohttp.com/en/method) · [Articles (en)](https://www.geohttp.com/en/articles)

## Requirements and usage

- Python 3.8+
- **Standard library only** (no requests, BeautifulSoup, or lxml)
- HTTP GET only — does not write the target site or its database
- Source comments/docstrings are in English; the default Markdown report is Chinese (same as the zh site)
- Do not commit `site_profiles.local.json` or `.geo_new_session` (listed in `.gitignore`)

```bash
# Audit one site
python3 03-geo_check.py --site https://www.example.com/

# JSON output
python3 03-geo_check.py --site https://www.example.com/ --json

# Write a Markdown report
python3 03-geo_check.py --site https://www.example.com/ --out report.md

# Run the default demo list (example.com / geohttp.com)
python3 03-geo_check.py --all
```

For private batch lists, put URLs in `site_profiles.local.json` next to the script (or point `GEO_CHECK_SITES` at a JSON file). **Do not commit that file to a public repository.**

Exit codes: `0` ok; `2` one or more P0 issues (handy for cron / CI).

## Relation to the live site

| | Role |
|---|---|
| **Live site** | [www.geohttp.com](https://www.geohttp.com) — web submit, multi-view reports, articles, scoring docs |
| **This repo** | Open-source core algorithm; run locally or wire into scripts; same judges as production |

Want results first: open **[geohttp.com](https://www.geohttp.com)**. Want local replay or automation: use this repository.

## License

GNU Affero General Public License v3.0 (AGPL-3.0)

---

# 中文

网站 GEO（生成式引擎优化）自动体检：检查公开网页是否容易被 AI 爬虫抓取、理解并引用。

## 在线体验（推荐）

本仓库是命令行核心算法。线上完整服务在：

**[https://www.geohttp.com](https://www.geohttp.com)**（GEO库 / GEO Cool）

打开网站，粘贴一个公网 URL，即可得到：

- 0–100 分与分档
- 五轴明细、证据与修改顺序
- 爬虫实测、全量目录、观察项等多视图报告

无需安装、无需账号即可试用。

- 英文镜像：[https://www.geohttp.com/en](https://www.geohttp.com/en)
- 计分方法：[https://www.geohttp.com/method](https://www.geohttp.com/method)
- 英文计分说明：[https://www.geohttp.com/en/method](https://www.geohttp.com/en/method)

本仓库中的 `03-geo_check.py`，就是 [geohttp.com](https://www.geohttp.com) 线上体检实际调用的判定逻辑（五轴 v11）。

## 五轴评分体系（合计 100）

基础分按五个轴加权。抓取失败记为「无法判定」：不按 0 分打进分子，只降低置信度。微调与 P0 封顶在基础分之外处理。

| 轴 | 满分 | 在看什么 |
|---|---:|---|
| 引用可达 | 28 | 引用型爬虫显式放行、实测 HTTP 200、sitemap |
| 结构化数据 | 18 | JSON-LD 合法、关键类型、FAQPage / QAPage |
| 内容可引用 | 24 | 答案前置、表格或权威外链、时间与价格日期；加分池最多 5 |
| 页内实体 | 18 | Organization、sameAs、@id/profile、可见作者与 FAQ |
| 技术与渲染 | 12 | 无嵌套文档、lang、canonical、社交标签、压缩、HTTP/2 |

### 1. 引用可达 · 28

| 项目 | 满分 | 说明 |
|---|---:|---|
| 引用型爬虫显式 Allow | 12 | 核心集：OAI-SearchBot、Claude-SearchBot、PerplexityBot、Googlebot、Applebot；整站 `Disallow: /` 为 P0 |
| AI 爬虫实测可达 | 10 | 用代表性 UA 再请求提交的 URL；引用/检索型须返回 200 |
| sitemap 有效且已声明 | 6 | 根上 sitemap 可打开，且 robots 中有 `Sitemap:` 行 |

训练型爬虫（如 GPTBot、ClaudeBot、CCBot）放不放由站点自定，被拦不记入失败。

### 2. 结构化数据 · 18

| 项目 | 满分 | 说明 |
|---|---:|---|
| JSON-LD 存在且合法 | 8 | 至少一段可解析的 `application/ld+json`；语法错误整项失败 |
| 关键类型齐全 | 5 | Organization / WebSite / Article / Product / BreadcrumbList / BlogPosting，命中 4 类满分 |
| 问答 FAQPage / QAPage | 5 | 页面真有问答结构时再加；首页无 FAQ 属正常 |

实体相关分已拆到「页内实体」轴，避免与类型、问答重复计分。

### 3. 内容可引用 · 24

| 项目 | 满分 | 说明 |
|---|---:|---|
| 答案前置或问答 | 8 | 主内容有可单独引用的结论段，或 FAQ / QA 结构 |
| 数据表或权威外链 | 6 | 带表头的表，或链到政府 / 教育 / 百科 / 厂商等权威站 |
| 时间与价格日期 | 5 | `time` + datetime；若有价格，须能看出对应日期 |
| 内容加分池 | ≤5 | 标题长度、meta description、标题层级、问句标题、favicon、懒加载位置、脚本预算等，每项 1 分，整池封顶 5 |

### 4. 页内实体 · 18

| 项目 | 满分 | 说明 |
|---|---:|---|
| Organization 声明 | 4 | JSON-LD 中声明 Organization |
| sameAs 权威锚点 | 5 | 至少两条 sameAs（百科、官微、登记或其它官网） |
| @id 与 name/url | 3 | 稳定 `@id`，并同时有 name、url |
| 可见作者署名 | 3 | 页面上看得到作者信息 |
| 可见 FAQ / 问答 | 3 | 可见问答区块（与结构化 FAQ 类型互补） |

### 5. 技术与渲染 · 12

| 项目 | 满分 | 说明 |
|---|---:|---|
| 无嵌套文档 | 3 | 整页只有一套 html / head / DOCTYPE |
| 语言声明 | 2 | `html` 带 `lang` |
| canonical | 2 | 有指向本页的规范链接 |
| Open Graph / Twitter | 2 | 社交标签合计达到门槛 |
| 压缩 | 2 | gzip 或 brotli |
| HTTP/2 | 1 | 协议版本为 HTTP/2 |

## 微调、封顶与分档

| 规则 | 说明 |
|---|---|
| 发现文件 | 根上 llms.txt、ai.txt 或 `/ai/*.json` 等，最多 **+5** |
| 注入自查 | 高信号注入短语、`data-ai-` 类属性、零宽字符等，最多 **−5** |
| 合计限制 | 微调合计限制在 **−5 ~ +5** |
| P0 封顶 | 仍有未修复的 P0 时，最终分 **不超过 74** |
| 无法判定 | 超时、连接失败 → unknown，不按 0 分进分子，只拉低置信度 |

分档：90+ 优秀 · 75–89 良好 · 50–74 及格 · 低于 50 待整改。

## 多维度检测（不止计分行）

除五轴计分项外，脚本还会做下列检测；部分进入问题清单或报告展示，**不单独占基础分 100**（与官网「观察项 / 全量目录」一致）。

### 爬虫与平台

- 引用型、检索型、训练型、经典搜索引擎 UA 清单与 robots 策略
- 多 UA 实测连通性
- 平台可读性矩阵（如 Google AI 概览、ChatGPT 搜索、Perplexity、Claude、Copilot 等）——展示用，不重复计入 100 分

### 页面与内容观察

- 主标题 h1、语义标签、meta 是否误放在 body
- 安全响应头
- 完整更新日期、现代图片格式、DOM 体积提示
- 标题 / description 长度、favicon、标题层级、问句型标题
- 懒加载位置、脚本预算
- llms.txt 质量提示
- 训练爬虫策略、Crawl-delay（观察）
- Agent 图片尺寸（宽高提示）

### 发现与安全

- AI 发现端点：llms.txt、`/.well-known/ai.txt`、`/ai/summary.json` 等
- GEO 注入 / 隐藏文字自查

问题按 **P0 → P3** 分级。P0 会切断引用链（例如引用型爬虫被整站挡住、缺 lang、JSON-LD 缺失或非法、文档嵌套等）。

更完整的口径说明见官网：[计分方法](https://www.geohttp.com/method) · [文章](https://www.geohttp.com/articles)

## 要求与用法

- Python 3.8+
- **仅标准库**（无需 requests、BeautifulSoup、lxml）
- 只发 HTTP GET，不写对方站点、不改对方数据库
- 源码注释与 docstring 为英文；命令行 Markdown 报告默认中文（与官网中文站一致）
- 请勿提交 `site_profiles.local.json`、`.geo_new_session`（已列入 `.gitignore`）

```bash
# 检测单个站点
python3 03-geo_check.py --site https://www.example.com/

# 输出 JSON
python3 03-geo_check.py --site https://www.example.com/ --json

# 写出 Markdown 报告
python3 03-geo_check.py --site https://www.example.com/ --out report.md

# 跑默认示例清单（example.com / geohttp.com）
python3 03-geo_check.py --all
```

批量自测可把自有 URL 写在同目录 `site_profiles.local.json`（或环境变量 `GEO_CHECK_SITES` 指向的 JSON），**不要把该文件提交到公开仓库**。

退出码：`0` 正常；`2` 存在 P0 问题（便于 cron / CI）。

## 与官网的关系

| | 说明 |
|---|---|
| **官网** | [www.geohttp.com](https://www.geohttp.com) — 网页提交、多视图报告、文章与计分说明 |
| **本仓库** | 开源核心算法；可本地命令行运行，与官网判定同源 |

想先看结果：直接打开 **[geohttp.com](https://www.geohttp.com)**。要本地复现或接入脚本：用本仓库。

## 许可证

GNU Affero General Public License v3.0（AGPL-3.0）
