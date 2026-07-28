# Lab Literature Intelligence 实验室文献情报系统 · 具体行动方案

> 版本：V4（按当前工作区实际状态编写）· 更新日期：2026-07-28
> 部署环境：macOS · 项目根目录 `~/lab-literature-intelligence` · Python 3.13，一律使用 `.venv/bin/python`
> 注意：V4 粗筛改造（等权打分 / 绝对阈值定级 / 自动词表）目前在**工作区未提交**；
> 已提交的最新 commit 为 `95ef5e7`（docs: add project status document）。
> 测试基线：130 passed（2026-07-28 复跑验证）。

---

## 1. 系统目标

面向实验室多用户的个性化文献情报系统。每天自动抓取 PubMed / bioRxiv 最新文献，
按每个成员的个人画像（species / methods / research_interest / keywords 四组检索词
\+ 手配 aliases 同义词 \+ LLM 自动扩展词 \+ 反馈学习词表）经三层漏斗筛选排序，
以邮件发送**日报（默认 15 篇，六维加权定级）**与**周报（近 7 天聚合）**，
并通过邮件回信反馈形成**全自动学习闭环**：LLM 先为个人检索词做语义拓展（召回兜底），
反馈标注后 AI 再从重要文献中提炼新词入库——全程无需人工审核。

---

## 2. 总体架构

```
采集层 sources/                 排序层 recommendation/            LLM 处理层 processing/
  pubmed.py（严格/宽松降级）──→    scorer.py（等权规则粗筛）──→      analyzer.py（结构化分析）
  biorxiv.py（全量+本地过滤）     ranker.py（六维精排+定级）         paper_news_generator.py（新闻摘要）
       │                              │                           translator.py（中英翻译）
       ▼                              ▼                           daily_summary_generator.py（每日总结）
              database/db.py（SQLite：papers / paper_analysis / paper_news_summary
                                 / recommendations / feedback / learned_terms）
                                    │
        ┌───────────────────────────┼─────────────────────────────┐
        ▼                           ▼                             ▼
  mailer/digest_builder.py   weekly_report.py（周报）        feedback/（学习闭环）
  （日报三段式 HTML）         （统计+趋势+清单，三段式）       collector.py  IMAP 收集 [FB] 回信
        │                                                     learner.py    全自动词表学习
        ▼                                                     vocab.py      半衰期衰减读取
  mailer/sender.py（SMTP 发信）

召回增强（V4）：processing/term_expander.py 每日自动维护
  config/users/auto_terms/<slug>.yaml（LLM 扩展词 expansion + 反馈新增 feedback_added）
```

调度入口：`run_daily.sh`（cron，先 `python -m feedback` 收集学习、再 `python main.py` 发日报，
顺序执行非 &&，反馈失败不阻断日报）；周报入口 `run_weekly.sh`（`weekly_report.py`）。

---

## 3. 目录结构与关键文件

| 路径 | 职责 |
|---|---|
| `main.py` | 每日流水线总入口：遍历 `config/users/` 所有 active 用户逐人执行；参数 `--user` / `--days`（默认 1）/ `--limit`（默认取 email.yaml 的 daily_paper_number=15）/ `--dry-run`（不发信不写库，HTML 落 `logs/`）；单用户失败 try/except 隔离继续下一个 |
| `weekly_report.py` | 周报入口：`--user` / `--days 7` / `--dry-run`；聚合 SQLite 近 7 天推荐 → 统计 → LLM 趋势总结 → 三段式周报 |
| `sources/pubmed.py` | PubMed 采集（NCBI E-utilities）。`build_queries` 返回（严格， 宽松）两条查询：严格=物种组 OR 与其余全部检索词 OR 做 AND；宽松=全部词扁平 OR；严格命中 <5 篇自动降级宽松；两端都附加 exclude 词的 NOT 排除；aliases 与有效学习词先并入检索词；429 限流指数退避（5s/10s/20s） |
| `sources/biorxiv.py` | bioRxiv 采集：无服务端检索，按日期区间拉全量后本地用同一套词表语义过滤 |
| `sources/paper.py` | Paper 数据类 + `expand_with_aliases`（检索词并入 aliases 变体）+ `dedupe`（DOI 优先、规范化标题兜底） |
| `recommendation/scorer.py` | 三层漏斗第 2 层：零成本等权关键词粗筛 + tie-break；exclude 命中直接剔除；期刊分层只在此加载（供精排用），**粗筛不按期刊加分**（V4）；叠加 learned 词加分（单篇封顶 6） |
| `recommendation/ranker.py` | 三层漏斗第 3 层：六维 0-100 加权 Final Score + AI 中文推荐理由；按绝对阈值定级（≥75 Must Read / ≥60 Important / 其余 Reference，宁缺毋滥不凑配额）；LLM 异常回退中性分 50 |
| `processing/term_expander.py` | V4 召回增强核心：每日 `refresh_auto_terms` 按需刷新 LLM 扩展词缓存、`apply_auto_terms` 并入用户配置副本；另保留离线人工审核工具模式（见第 8 节） |
| `processing/llm.py` | LLMClient：读 `config/model.yaml`（provider openai / model gpt-5.4 / temperature 0.2），`OPENAI_API_KEY` 与可选 `OPENAI_BASE_URL` 来自 `.env`；temperature 参数被网关拒绝时自动去参重试；护栏：timeout 60s / max_tokens 2000 / 可重试错误（429·连接·超时·5xx）5s/10s/20s 退避重试 3 次 / daily_budget 1000（跨进程日预算，记录 `logs/.llm_usage.json`，≤0 不限） |
| `processing/analyzer.py` | 论文结构化分析（problem / solution / finding / methods / organisms），prompt `paper_analysis.txt` |
| `processing/paper_news_generator.py` | 一句话新闻摘要（解决什么问题→什么方法→什么创新→什么结果） |
| `processing/translator.py` | 标题+摘要中文翻译（由 email.yaml `show_translation` 控制） |
| `processing/daily_summary_generator.py` | 日报末尾"今日价值总结"（LLM 生成） |
| `database/db.py` | SQLite（`literature_agent.db`）：papers（dedup_key UNIQUE，全局共享）、paper_analysis、paper_news_summary、recommendations（user_email+paper_id UNIQUE，sent_date/category/score）、feedback（UNIQUE(user,paper,value)，processed 标记）、learned_terms（weight/support/last_seen）；全部幂等写入 |
| `mailer/digest_builder.py` | 日报三段式 HTML（模板 `templates/daily_digest.html`）；卡片底部反馈 mailto 链接（主题带 `[FB]` token） |
| `mailer/sender.py` | SMTP 发信：凭据全部来自 `.env`（SMTP_HOST/PORT/USER/PASSWORD/DIGEST_FROM_EMAIL），缺失即报错列出缺项；465 走 SSL，其余 starttls |
| `feedback/collector.py` | IMAP 轮询发件箱，解析 `[FB] u=<邮箱> p=<id> v=<值>` 回信写 feedback 表并标记已读；需 `.env` 配 IMAP_HOST（IMAP_USER/PASSWORD 缺省回退 SMTP 配置） |
| `feedback/learner.py` | 全自动学习：正反馈 LLM 提炼新词（≥2 篇支持才提权）、负反馈只降权命中的学习词（×0.5）、already_read 只记录；审计 `logs/feedback_learning.log`（JSONL） |
| `feedback/vocab.py` | 学习词读取侧：有效权重=原始权重×0.5^(天数/半衰期 30 天)，低于 0.3 失效；读取时计算不回写 |
| `config/users/<slug>.yaml` | 个人画像：name/email/active + 四组检索词 + exclude + aliases；**新增成员只需加一个 yaml，不改代码** |
| `config/users/auto_terms/<slug>.yaml` | V4 自动词表缓存（自动维护勿手改，已 gitignore）：expansion（每词 ≤5 个别名）+ feedback_added |
| `config/lab.yaml` | 实验室公共方向 topics + aliases，`apply_lab_profile` 并入每个用户（个人配置优先） |
| `config/scoring.yaml` | 全部打分行为开关：粗筛等权/tie-break、ranker 六维权重与定级阈值、learned 学习护栏（详见第 4、9 节） |
| `config/journals.yaml` | 期刊分层 T0（CNS 正刊+顶级子刊 22 本）/ T1（综合强刊、基因组、进化生态、昆虫领域强刊）；仅用于精排 journal 维度与周报统计 |
| `config/email.yaml` | daily_paper_number 15 / show_translation / show_keywords / show_doi |
| `prompts/` | 8 个 prompt 文件：paper_analysis / paper_news_summary / paper_translation / daily_value_summary / recommendation_reason / feedback_term_extraction / term_expansion / weekly_report |
| `run_daily.sh` / `run_weekly.sh` | cron 入口脚本；日志 `logs/pipeline.log`、`logs/cron.log` |
| `tests/` | 130 个测试全 mock 无网络依赖，`.venv/bin/python -m pytest tests/ -q` |

---

## 4. 评分体系（三层漏斗）

### 第 1 层：检索召回（sources/）
PubMed 严格查询（物种 OR）AND（其余词 OR），命中不足 5 篇降级为全词扁平 OR；
bioRxiv 按日期全量拉取后本地同词表过滤；两源合并按 DOI/标题去重（PubMed 已发表版优先）；
exclude 词在查询端 NOT 排除。

### 第 2 层：规则粗筛（`scorer.py` + `config/scoring.yaml`）
零成本关键词匹配，把零相关论文挡在昂贵的 LLM 处理之外：

| 规则 | 分值 | 说明 |
|---|---|---|
| 检索词命中 | +1/词 | 五组词表（species/methods/research_interest/keywords/lab_topics）全部等权；原词或其 aliases/自动扩展变体任一命中即计，多变体不重复计 |
| 标题命中 | +1 | 变体出现在标题（title_bonus） |
| 命中频次 | +1/次 | 按命中最多的变体计，封顶 3 次（frequency_bonus/frequency_cap） |
| 学习词命中 | +有效权重 | 单篇封顶 6 分（learned.score_cap，不压过手配词表） |
| exclude 命中 | 剔除 | 直接淘汰 |

粗筛只负责排序选出 top-N（默认 15）候选，**不定级、不含期刊因素**。

### 第 3 层：个性化精排（`ranker.py`）
六维各 0-100，按权重加权为 Final Score（0-100）：

| 维度 | 权重 | 计算方式 |
|---|---|---|
| personal 个人相关度 | 30 | LLM 依据个人画像语义判断（异常回退 50） |
| lab 实验室方向 | 25 | lab_topics 命中 0/1/≥2 个 → 0/50/100 |
| journal 期刊影响力 | 20 | journals.yaml 分层：T0=100 / T1=70 / 未分层=30（刊名规范化后精确匹配） |
| novelty 新颖性 | 10 | LLM 依据 AI 分析判断（异常回退 50） |
| method 方法相关度 | 10 | 个人 methods 命中 0/1/≥2 个 → 0/50/100 |
| recency 时效性 | 5 | 当天~1 天=100 / 2 天=80 / 3 天=60 / 周内=40 / 更早=20 / 日期无法解析=50 |

**定级**：Final Score ≥75 → Must Read；≥60 → Important；其余 Reference。
绝对阈值、宁缺毋滥——当日全部低分则可以没有 Must Read，不按固定配额凑数（V4 替代旧配额定级）。
同时 LLM 生成一句中文推荐理由展示在卡片上。

---

## 5. 每日流水线（`run_daily.sh` → `main.py`）

```
python -m feedback            # 先收集回信并学习（见第 9 节）
python main.py                # 再对每个 active 用户逐人执行：
  1. 加载反馈学习词表（vocab.load_active_terms，半衰期衰减后 ≥0.3 才生效）
  2. 加载/刷新自动词表 auto_terms（扩展词+反馈新增词并入配置副本，见第 8 节）
  3. PubMed 严格/宽松降级检索 + bioRxiv 全量本地过滤 → 合并去重
  4. 规则粗筛等权打分（lab_topics 叠加个人词表；期刊不参与）
  5. 跨天去重：该用户历史已发论文（recommendations 表）跳过
  6. 取 top-N（默认 15）进入 AI 处理
  7. 逐篇 LLM：结构化分析 → 新闻摘要 → 中文翻译（可被 show_translation 关闭）
  8. 六维精排 + 生成推荐理由 → 按绝对阈值定级
  9. 入库 SQLite（论文/分析/摘要全局共享，推荐记录按用户隔离）
  10. 生成今日价值总结 → 渲染三段式 HTML → SMTP 发送
     主题：Daily Literature Intelligence Report · <日期>
     dry-run：不发信不写库，HTML 写 logs/digest_<日期>_<用户>.html
```

单用户任何环节异常只记录日志并继续下一个用户，不中断整体。

## 6. 每周流水线（`run_weekly.sh` → `weekly_report.py`）

```
weekly_report.py --days 7（--user 可选，--dry-run 不落邮件）
  聚合 recommendations ⋈ papers ⋈ news 近 7 天记录
  → compute_stats：定级分布 / 期刊分层 / Top 期刊 / 高频关键词
  → LLM 趋势总结（仅基于 Must Read / Important）
  → 三段式周报邮件
```

---

## 7. 邮件规格

**日报（三段式）**：Part 1 今日论文新闻摘要表（一句话：问题→方法→创新→结果）；
Part 2 详细卡片（定级徽标、Final Score、中英文标题摘要、Keywords、DOI、AI 推荐理由、
四个反馈链接：相关/不相关/已读/收藏）；Part 3 今日价值总结。

**周报（三段式）**：Part 1 LLM 趋势总结；Part 2 分布统计（定级/期刊分层/Top 期刊/高频词）；
Part 3 重点论文清单。

**反馈链接**：mailto 回信，主题 `[FB] u=<用户邮箱> p=<论文id> v=<relevant|not_relevant|already_read|save>`，
正文首段可自由填写原因（可选，只标注不填原因同样生效）。

**发信**：SMTP（凭据在 `.env`，不入库不入文档），465 SSL 或 starttls。

---

## 8. 自动词表（V4 召回增强：替代人工审核的全自动拓展）

每日流程中 `term_expander.refresh_auto_terms` 为每个用户维护
`config/users/auto_terms/<slug>.yaml`（自动维护，勿手改，已 gitignore），含两块：

- **expansion**：LLM 一次性为个人全部检索词生成同义词/拉丁学名/缩写等别名
  （每词 ≤5 个，prompt `term_expansion.txt`），只为召回兜底、与手配词等权参与检索与粗筛，
  **不写回个人 yaml**；
- **feedback_added**：反馈闭环确认的新关键词，等权追加进 keywords。

**刷新触发**：缓存缺失 / 用户 yaml 比缓存新 / 缓存超过 7 天。
**失败策略**：LLM 异常或输出非法时沿用旧缓存，流水线不中断。
**合并优先级**（`apply_auto_terms`，只改配置副本）：个人 aliases 在前优先，扩展词在后，
大小写不敏感去重。

另保留离线人工审核模式（非每日流程）：`python -m processing.term_expander config/users/user001.yaml`
只打印 aliases 建议到 stdout，人工审核后自行合并，工具不改任何文件。

---

## 9. 反馈学习闭环（全自动）

1. 用户在日报卡片点 **相关 / 不相关 / 已读 / 收藏** → 邮件客户端生成 `[FB]` 回信草稿发出；
   正文首段可填原因（非必填）。
2. 每日流水线**之前** `python -m feedback`：IMAP 收取未读 `[FB]` 回信 → 写 feedback 表
   （幂等：同人同篇同评级去重）→ 标记已读 → 执行 `learner.learn_from_feedback`：
   - `relevant` / `save`：LLM 从论文提炼候选新词；同一词需 **≥2 篇**高分论文支持才提权
     （初始 1.0，每多一篇 +0.5，上限 3.0）——防漂移护栏；
   - `not_relevant`：**只**对该篇命中的学习词降权（×0.5），不动手配词表、不写 exclude；
   - `already_read`：只记录，不调整。
3. 学习词参与次日检索与粗筛：读取时按 30 天半衰期衰减，有效权重 <0.3 视为失效；
   全部调整写审计日志 `logs/feedback_learning.log`（JSONL，可回滚）。

护栏参数全部在 `config/scoring.yaml` 的 `learned` 节，调行为只改配置。

---

## 10. 运维速查

```bash
cd ~/lab-literature-intelligence

.venv/bin/python -m pytest tests/ -q        # 全量测试（当前 130 passed）
.venv/bin/python main.py --dry-run          # 全用户试跑：不发信，HTML 落 logs/
.venv/bin/python main.py --user user001 --days 3   # 单用户调试，回溯 3 天
.venv/bin/python weekly_report.py --dry-run        # 周报预览
.venv/bin/python -m feedback --learn-only          # 跳过 IMAP 只重跑学习
tail -f logs/pipeline.log                          # 流水线日志
```

crontab 示例（日报每天、周报每周一）：

```cron
30 7 * * *   cd ~/lab-literature-intelligence && ./run_daily.sh >> logs/cron.log 2>&1
53 7 * * 1   cd ~/lab-literature-intelligence && ./run_weekly.sh >> logs/cron.log 2>&1
```

- 新增用户：复制 `config/users/user001.yaml` 改 name/email/词表即可，无需改代码；
  次日自动获得 LLM 扩展词缓存。
- 调推荐行为：只改 `config/scoring.yaml`（粗筛/精排权重、定级阈值、学习护栏）、
  `config/journals.yaml`（期刊分层）、`config/email.yaml`（篇数与展示开关），不改代码。
- 换模型：改 `config/model.yaml` + `.env` 的 `OPENAI_API_KEY` / `OPENAI_BASE_URL`。
- 密钥全部在 `.env`（SMTP/IMAP/OPENAI），已 gitignore；`.env.example` 为模板。

---

## 11. 待办与已知问题

来源：`docs/OPTIMIZATION_REPORT.md`（全量审查，基准 commit `b3b701c`，即 V4 改造前）。
V4 已完成：等权粗筛、期刊因素移入精排、绝对阈值定级替代配额定级、
journal_fallback 低相关兜底退役、LLM 自动扩展词每日缓存。

| # | 事项 | 优先级 | 状态 |
|---|---|---|---|
| 1 | ~~反馈收集不校验发件人 From，任何人可伪造 `[FB]` 回信污染他人学习词表（中危安全漏洞，`feedback/collector.py`）~~ | P0 | **已修**（2026-07-28）：collect() 校验发件人与主题标注用户一致，不符拒绝记录 |
| 2 | LLM 产物（分析/新闻/翻译）按用户重复计算，全局表只写不读；50 用户时成本/时长放大 5–10 倍 | P0 | 待修（全局摄取层+产物缓存复用，即批 2 全局池） |
| 3 | ~~LLMClient 无 max_tokens/超时/429 退避/日预算护栏~~ | P0 | **已修**（2026-07-28）：timeout/max_tokens/退避重试/日预算全部落地，参数在 `config/model.yaml`；单篇 LLM 失败丢该篇但整库未增量持久化（剩余子项，随批 2 解决） |
| 4 | ~~`main()` 恒返回 0，流水线失败 cron 无感知（静默失败）~~ | P0 | **已修**（2026-07-28）：`main.py` 与 `weekly_report.py` 任一用户失败返回 1 |
| 5 | 关键词为子串匹配无词边界（如 "ant" 误中 "plant"）；learned 词入库未净化 | P1 | 待修 |
| 6 | 无 users 表（email 字符串为身份键）；SQLite 未开 WAL/busy_timeout、无 schema 版本 | P1 | 待修 |
| 7 | 召回纯关键词、无语义检索层（无 embedding）；新用户冷启动依赖手写词表 | P1 | 待修（语义通道+RRF 融合） |
| 8 | 死结构：`scheduler/` 空目录、`schedule` 死依赖、`.env.example` 的 `OPENAI_MODEL` 死配置、依赖未钉版本 | P1 | 待清理（`scheduler/` 将由批 3 的节假日模块启用） |
| 9 | 反馈深度不足：`reason` 文本未分析、`already_read` 未作弱信号、排序权重静态不学习 | P2 | 待演进 |
| 10 | 数据源仅 PubMed+bioRxiv；N/C/S RSS、arXiv 未接入；测试全 mock 无集成测试 | P2 | 待演进 |

---

## 12. 总体行动方案与下一步（2026-07-28 四项决策全部确认）

### 12.1 决策落地规格

**决策 1 · 反馈通道 = 邮件回信（路径 A）**：部署机器不能被其他用户设备访问，
不引入任何本地 HTTP 服务，零入站端口；反馈继续走 `[FB]` mailto 回信 + IMAP 收集。
安全攻击面仅剩 SMTP/IMAP/OPENAI 凭据（`.env` + gitignore）与已修复的 From 伪造校验。

**决策 2 · 五星反馈**（替代 relevant / not_relevant / already_read / save 四键）：
卡片底部 5 个链接 ⭐1–⭐5，主题 `[FB] u=<邮箱> p=<id> v=<1..5>`；
正文 `+关键词1, 关键词2` 语法保留（写入 `auto_terms` 的 `feedback_added`，次日进检索）。
星级→学习信号映射（最小改动复用现有学习逻辑，参数进 `config/scoring.yaml` learned 节）：

| 星级 | 语义 | 学习行为 |
|---|---|---|
| ⭐5 / ⭐4 | 正（≈旧 relevant/save） | LLM 提炼新词，沿用 ≥2 篇支持才提权护栏（初始 1.0 / +0.5 / 上限 3.0） |
| ⭐3 | 中性（≈旧 already_read） | 只记录，不调整 |
| ⭐2 | 负 | 该篇命中的学习词 ×0.5 |
| ⭐1 | 强负 | 该篇命中的学习词 ×0.25，审计标注；同词累计 2 次 ⭐1 进人工排除候选 |

**决策 3 · 精排六维权重**：journal 30 / personal 20 / lab 20 / method 10 / novelty 10 / recency 10
（合计 100，结构不变只改 `config/scoring.yaml` ranker.weights，并对齐 `ranker.py` 默认值）。
口径说明：按分组标签"规则 40（lab+method）/ LLM 20（personal+novelty）"与六维数值
（实为规则 30 / LLM 30）存在 10 分差异；若要严格 30/40/20/10 四桶则需
lab 30 / method 10 / personal 10 / novelty 10。**实施 B1 前需用户一句话确认采用哪套**，
默认按六维数值（personal 语义判断是个性化质量核心，不建议压到 10）。

**决策 4 · 节假日静态表**：

```yaml
# config/holidays.yaml —— 中国法定节假日，每年初按国务院放假安排手工维护一次
holidays: ["2026-01-01", "2026-02-16", "..."]
```

- 新增 `scheduler/holiday.py`：`backfill_days(today)` —— 当日在表内返回 0（跳过）；
  否则返回 1 + 当日之前连续节假日天数（上限 10，覆盖节后合并补发空窗）。
- `run_daily.sh` 前置：`DAYS=$(.venv/bin/python -m scheduler.holiday)`，为 0 记日志退出，
  否则 `python main.py --days $DAYS`；反馈收集随之一并跳过，节后首个工作日处理，无损失。
- `run_weekly.sh` 同样前置检查，逢节假日当周跳过（下周周报窗口自然滚动覆盖）。

### 12.2 三批实施路线

| 批次 | 内容 | 状态 |
|---|---|---|
| 批 1 P0 修复 | #1 collector 防伪造、#3 LLMClient 护栏、#4 失败返回非 0；130 passed | ✅ 已完成（2026-07-28） |
| 批 2 全局池 | 全局词表合并 → LLM 主题聚类（7 天缓存）→ 每簇一条 PubMed 查询合并全局池 → 按人本地粗筛分发；LLM 产物按并集 shortlist 只算一次并 SQLite 缓存复用（解决第 11 节 #2、#3 剩余子项） | 进行中 |
| 批 3 产品功能 | B6 中文四段结构化摘要 → B7 邮件开头总览 → B1 权重落地 → B5 节假日静态表 → B3 月报（30 天阅读趋势+领域总结）→ B2+B4 五星反馈与关键词回信语法 | 待实施 |

### 12.3 批 4 候选（第 11 节 P1/P2 余项）

#5 关键词词边界匹配 + learned 词入库净化；#6 users 表 + SQLite WAL/busy_timeout + schema 版本；
#7 embedding 语义召回通道 + RRF 融合（解决冷启动与同义词漏召回）；#8 死结构清理
（`schedule` 死依赖、`.env.example` 死配置、依赖钉版本，`scheduler/` 由 B5 启用）；
#9 反馈深度（reason 文本分析、⭐3 弱信号、排序权重学习）；#10 数据源扩展（arXiv / NCS RSS）与集成测试。
