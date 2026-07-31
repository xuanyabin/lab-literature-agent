# Lab Literature Intelligence 实验室文献情报系统 · 具体行动方案

> 版本：V4（按当前工作区实际状态编写）· 更新日期：2026-07-30
> 部署环境：macOS · 项目根目录 `~/lab-literature-inteligence` · Python 3.13，一律使用 `.venv/bin/python`
> 状态：批 1–3、批 5 全部落地并提交；批 6（2026-07-29）：邮件卡片内嵌 ⭐1–⭐5 五星反馈链接
> （HTTP 反馈服务 `feedback/server.py` 点星即记录、浏览器显示"已反馈"，HMAC token 防伪造；
> 未配置 FEEDBACK_BASE_URL/FEEDBACK_SECRET 时自动回退为批量回信标注）。
> 批 7（2026-07-29）：顶刊直采通道（`sources/top_journals.py`，按 journals.yaml 刊名直抓
> 绕过关键词召回，解决全局池零 CNS）+ 推送下限（`ranker.thresholds.push_floor`，低分论文不进邮件）。
> 批 11（2026-07-29）：性能改造——LLM 并发（model.yaml `max_workers=8`，产物生成与精排批次并行）、
> 精排批处理（scoring.yaml `ranker.batch_size=5`，一次 LLM 调用评判多篇）、token 计量
> （`logs/.llm_usage.json` 累计 prompt/completion/total tokens，流水线结束打用量日志）。
> 批 12（2026-07-30）：lab.yaml 词表拓展（31 → 203 topics，覆盖 55 个课题方向，
> 全部经 PubMed 90 天命中验证，过宽词有意不收）+ lab_topics 并入全局检索词参与召回
> （此前只参与打分，实验室方向文献可能漏召回）。
> 批 13（2026-07-30）：修复批 12 副作用——lab_topics 不再参与粗筛打分（203 个全员
> 共享词等权计入淹没个人词区分度，各用户候选趋同），只在全局召回与精排 lab 维度
> 生效；精排 lab 维度改为四档梯度（命中 0/1/2/3/≥4 → 0/25/50/75/100），
> 防大词表下 ≥2 即满分饱和。
> 批 14（2026-07-30）：lab.yaml 按 `lab_literature.exmple.txt`（实验室实际关注文献
> 清单）整体重构为"演化框架 × 组学方法"短语词表（87 topics / 17 条 aliases，
> 11 个主题分组）。批 12 的 203 词版本此前已被裁剪，当前生效版本中的裸方法词/
> 裸概念词（synteny、centromere、transposable element、sex determination、
> Tissue architecture 等）单独命中海量非演化论文，全部删除或改写为演化框架短语；
> 候选词经 PubMed 90 天命中验证（`logs/lab_term_verify_v2.json`），并用示例文献
> 标题做命中回归：85 个真实标题 77 个 ≥1 命中（放过的 8 篇均为裸 AI/单细胞方法学，
> 有意不收），6 条无关负例全部 0 命中。
> 测试基线：238 passed（2026-07-30 复跑验证）。

---

## 1. 系统目标

面向实验室多用户的个性化文献情报系统。每天自动抓取 PubMed / bioRxiv 最新文献，
按每个成员的个人画像（species / methods / research_interest / keywords 四组检索词
\+ 手配 aliases 同义词 \+ LLM 自动扩展词 \+ 反馈学习词表）经三层漏斗筛选排序，
以邮件发送**日报（默认 15 篇，六维加权定级）**、**周报（近 7 天聚合）**与**月报（近 30 天聚合）**，
并通过邮件回信反馈形成**全自动学习闭环**：LLM 先为个人检索词做语义拓展（召回兜底），
⭐1–⭐5 反馈标注后 AI 再从重要文献中提炼新词入库、回信 `+关键词` 次日进检索——全程无需人工审核。
多用户共享一次全局检索与一次 LLM 处理（全局摄取层 + 产物缓存复用），50 用户不放大成本。

---

## 2. 总体架构

```
采集层 sources/                     排序层 recommendation/            LLM 处理层 processing/
  global_pool.py（全局摄取层）──→     scorer.py（等权规则粗筛）──→      artifacts.py（全局产物层）
    全用户词表合并 → LLM 主题         ranker.py（六维精排+定级          analyzer.py（结构化分析）
    聚类分簇检索 → 当日全局池          +push_floor 推送下限）          paper_news_generator.py（新闻摘要）
  top_journals.py（顶刊直采通道，                               translator.py（中文四段摘要）
    按刊名直抓绕过关键词召回）                                  daily_summary_generator.py（每日总结）
  pubmed.py（遗留逐人路径，不在主流水线）
  biorxiv.py（全局扁平 OR 过滤为主）
       │                              │
       ▼                              ▼
              database/db.py（SQLite：papers / paper_analysis / paper_news_summary
                                 / paper_translation / recommendations / feedback / learned_terms）
                                    │
        ┌───────────────────────────┼─────────────────────────────┐
        ▼                           ▼                             ▼
  mailer/digest_builder.py   weekly_report.py（周报/月报）       feedback/（学习闭环）
  （总览块+四段式日报 HTML，  （统计+阅读趋势+趋势总结+清单）    collector.py  IMAP 收集 [FB] 回信
   卡片内嵌 ⭐1-5 反馈链接）                              server.py     HTTP 五星反馈服务(8710)
        │                                                     tokens.py     链接 HMAC 签名/校验
        │                                                     learner.py    五星反馈学习
        ▼                                                     vocab.py      半衰期衰减读取
  mailer/sender.py（SMTP 发信）

召回增强（V4）：processing/term_expander.py 每日自动维护
  config/users/auto_terms/<slug>.yaml（LLM 扩展词 expansion + 反馈新增 feedback_added）

调度闸门：scheduler/holiday.py + config/holidays.yaml（节假日跳过、节后合并补发）
```

调度入口：`run_daily.sh`（cron，前置节假日判断；先 `python -m feedback` 收集学习、
再 `python main.py --days N` 发日报，顺序执行非 &&，反馈失败不阻断日报）；
周报入口 `run_weekly.sh`（`weekly_report.py --days 7`）、月报入口 `run_monthly.sh`（`--days 30`），
均前置节假日判断。

---

## 3. 目录结构与关键文件

| 路径 | 职责 |
|---|---|
| `main.py` | 每日流水线总入口（三阶段，详见第 5 节）：逐人词表准备 → 全局池一次检索 → 每用户本地粗筛取 top-N → shortlist 并集一次 LLM 处理 → 每用户精排发信；参数 `--user` / `--days`（默认 1）/ `--limit`（默认取 email.yaml 的 daily_paper_number=15）/ `--dry-run`（不发信不写库，HTML 落 `logs/`）；单用户失败 try/except 隔离继续，任一用户失败整体返回 1 |
| `weekly_report.py` | 周报/月报入口：`--user` / `--days 7`（月报 `--days 30`）/ `--dry-run`；聚合 SQLite 窗口内推荐 → 统计 + 阅读趋势 → LLM 趋势总结 → 四段式邮件 |
| `sources/global_pool.py` | 全局摄取层：全用户检索词（aliases 展开 + 有效学习词 + 实验室公共方向 lab_topics）合并去重 → LLM 主题聚类分簇（`prompts/topic_clustering.txt`，按词表哈希缓存 7 天，失败沿用旧缓存、无缓存回退单簇）→ 逐簇扁平 OR PubMed 检索（簇内关键词等权、命中任一词即召回，无 AND 分组）合并去重 + bioRxiv 全局过滤 + （可选）顶刊直采通道合并 → 当日全局池；exclude 不进全局检索（各用户粗筛时各自剔除）、查询式永不带 NOT |
| `sources/top_journals.py` | 顶刊直采通道（批 7）：`load_journal_names` 从 journals.yaml 按分层读原始刊名；`fetch_top_journals` 逐刊 `"<刊名>"[jour]` 直抓 PubMed 最近论文（单刊失败跳过、刊间 sleep 0.4s），绕过关键词召回解决顶刊漏召回（实测全局池零 CNS）；开关与参数见 scoring.yaml `journal_channel` |
| `sources/pubmed.py` | **遗留逐人路径（不在主流水线，仅测试/调试）**。PubMed 采集（NCBI E-utilities）。`build_queries` 返回（严格， 宽松）两条查询：严格=物种组 OR 与其余全部检索词 OR 做 AND；宽松=全部词扁平 OR；严格命中 <5 篇自动降级宽松；两端都附加 exclude 词的 NOT 排除；aliases 与有效学习词先并入检索词；429 限流指数退避（5s/10s/20s） |
| `sources/biorxiv.py` | bioRxiv 采集：无服务端检索，按日期区间拉全量后本地过滤——主流水线 `fetch_recent_global` 用全局词表扁平 OR（等权、命中任一词即保留）；逐人 `fetch_recent`（严格/宽松降级）为遗留路径，仅测试/调试。抓取窗口向前多覆盖一天（bioRxiv 按美东时间发布，北京时间当天只有零星篇数），重叠由跨天去重兜底 |
| `sources/paper.py` | Paper 数据类 + `expand_with_aliases`（检索词并入 aliases 变体）+ `dedupe`（DOI 优先、规范化标题兜底） |
| `recommendation/scorer.py` | 三层漏斗第 2 层：零成本等权关键词粗筛 + tie-break；exclude 命中直接剔除；期刊分层只在此加载（供精排用），**粗筛不按期刊加分**（V4）；叠加 learned 词加分（单篇封顶 6） |
| `recommendation/ranker.py` | 三层漏斗第 3 层：六维 0-100 加权 Final Score + AI 中文推荐理由；按绝对阈值定级（≥75 Must Read / ≥60 Important / 其余 Reference，宁缺毋滥不凑配额）；`thresholds.push_floor` 推送下限——低于该分的论文直接不进邮件（批 7）；LLM 异常回退中性分 50；LLM 判断批处理执行——`ranker.batch_size`（默认 5）篇一次调用（prompt `recommendation_reason_batch.txt`，整批非法或数量不符整批回退中性分、单条非法仅该条回退），批次间按 `max_workers` 并发（批 11） |
| `processing/artifacts.py` | 全局产物层：各用户 shortlist 并集的 AI 分析 / 新闻摘要 / 中文四段摘要一次生成、全用户复用——优先读 SQLite 全局缓存，未命中才调 LLM 并写回；未命中论文按 model.yaml `max_workers` 并发调 LLM（worker 只调 LLM，SQLite 读写全留主线程，批 11）；LLM 日预算耗尽快速失败（向上传播），其余异常逐篇回退空产物不丢篇 |
| `processing/term_expander.py` | V4 召回增强核心：每日 `refresh_auto_terms` 按需刷新 LLM 扩展词缓存、`apply_auto_terms` 并入用户配置副本；`add_feedback_terms` 把回信 `+关键词` 追加进 auto_terms 的 feedback_added（B4，见第 9 节）；另保留离线人工审核工具模式（见第 8 节） |
| `processing/llm.py` | LLMClient：读 `config/model.yaml`（provider openai / model gpt-5.4 / temperature 0.2），`OPENAI_API_KEY` 与可选 `OPENAI_BASE_URL` 来自 `.env`；temperature 参数被网关拒绝时自动去参重试；护栏：timeout 60s / max_tokens 2000 / 可重试错误（429·连接·超时·5xx）5s/10s/20s 退避重试 3 次 / max_workers 8（LLM 并发线程数，complete() 无状态可安全并发，批 11）/ daily_budget 1000（跨进程日预算，记录 `logs/.llm_usage.json`，≤0 不限）；用量文件同时累计 token 消耗（`{"date","calls","prompt_tokens","completion_tokens","total_tokens"}`，类级锁保护并发读写），`get_usage()` 供流水线结束汇报（批 11） |
| `processing/analyzer.py` | 论文结构化分析（problem / solution / finding / methods / organisms），prompt `paper_analysis.txt` |
| `processing/paper_news_generator.py` | 一句话新闻摘要（解决什么问题→什么方法→什么创新→什么结果） |
| `processing/translator.py` | 标题中译 + 中文四段结构化摘要（背景/研究方法/研究结果/研究意义，prompt `paper_translation.txt`；由 email.yaml `show_translation` 控制，开启时邮件只展示中文四段摘要） |
| `processing/daily_summary_generator.py` | 日报末尾"今日价值总结"（LLM 生成） |
| `database/db.py` | SQLite（`literature_agent.db`）：papers（dedup_key UNIQUE，全局共享）、paper_analysis、paper_news_summary、paper_translation（四段摘要，全局共享）、recommendations（user_email+paper_id UNIQUE，sent_date/category/score）、feedback（UNIQUE(user,paper,value)，processed 标记）、learned_terms（weight/support/last_seen）；全部幂等写入 |
| `mailer/digest_builder.py` | 日报 HTML（模板 `templates/daily_digest.html`）：开头总览块（窗口内全库新增/命中关键词/推送篇数与定级分布）+ 三段式；每篇论文一张卡片，默认显示一句话新闻概要，`<details>` 折叠作者/DOI/关键词/推荐理由/中文四段摘要（不支持的客户端默认全展开）；反馈按配置双模式——HTTP 模式（`.env` 配 FEEDBACK_BASE_URL+FEEDBACK_SECRET）：每张卡片底部内嵌 ⭐1–⭐5 链接（带 HMAC token，点击即由 feedback/server.py 落库），Part 3 收窄为"新增关注关键词"入口；回退模式（未配置）：Part 3 单个 mailto 按钮（主题 `[FB] u=<邮箱> d=<日期>`，正文预填当天编号行，按编号填 ⭐1–⭐5） |
| `mailer/sender.py` | SMTP 发信：凭据全部来自 `.env`（SMTP_HOST/PORT/USER/PASSWORD/DIGEST_FROM_EMAIL），缺失即报错列出缺项；465 走 SSL，其余 starttls |
| `feedback/server.py` | HTTP 五星反馈接收服务（`python -m feedback.server`，默认 0.0.0.0:8710，随 run_daily.sh 自动拉起）：`GET /fb` 点星即写 feedback 表并返回"已反馈"确认页（附关键词表单）；`GET /kw` 新增关键词表单/提交（写入 auto_terms feedback_added）；`GET /health`；所有写操作要求 HMAC token，缺 FEEDBACK_SECRET 拒绝启动 |
| `feedback/tokens.py` | 反馈链接 HMAC-SHA256 签名/校验（digest_builder 生成侧与 server 校验侧共用；签名覆盖用户|论文|星级，篡改任一参数即 403） |
| `feedback/collector.py` | IMAP 轮询发件箱，解析两种 `[FB]` 回信：批量格式 `u=<邮箱> d=<日期>`（正文编号行 `NN: 1-5`，编号经 recommendations 表按展示序映射回 paper_id）+ 旧逐篇格式 `u=<邮箱> p=<id> v=<1..5>`（向后兼容）；写 feedback 表并标记已读；From 防伪造校验（发件人须与主题标注用户一致）；正文 `+关键词` 行经 `add_feedback_terms` 入 auto_terms；需 `.env` 配 IMAP_HOST（IMAP_USER/PASSWORD 缺省回退 SMTP 配置）。HTTP 模式下为备用反馈通道 |
| `feedback/learner.py` | 全自动学习四档映射：⭐4/5 LLM 提炼新词（≥2 篇支持才提权）、⭐3 只记录、⭐2 命中学习词 ×0.5、⭐1 ×0.25 且同词累计 2 次写 exclude_candidate 审计；审计 `logs/feedback_learning.log`（JSONL） |
| `feedback/vocab.py` | 学习词读取侧：有效权重=原始权重×0.5^(天数/半衰期 30 天)，低于 0.3 失效；读取时计算不回写 |
| `config/users/<slug>.yaml` | 个人画像：name/email/active + 四组检索词 + exclude + aliases；**新增成员只需加一个 yaml，不改代码** |
| `config/users/auto_terms/<slug>.yaml` | V4 自动词表缓存（自动维护勿手改，已 gitignore）：expansion（每词 ≤5 个别名）+ feedback_added（反馈确认新词） |
| `config/holidays.yaml` + `scheduler/holiday.py` | 中国法定节假日静态表（每年初按国务院放假安排手工维护一次）；`backfill_days`：当日在表内返回 0（跳过），否则返回 1+当日之前连续节假日天数（上限 10，供节后合并补发）；CLI `python -m scheduler.holiday` 打印该整数 |
| `config/lab.yaml` | 实验室公共方向 topics（批 14：87 个"演化框架×组学方法"短语词，按示例文献 9 大主题分组，经 PubMed 90 天命中验证）+ aliases（17 条拼写变体）；`apply_lab_profile` 并入每个用户（个人配置优先），参与全局召回（批 12 起）与精排 lab 维度打分；不参与粗筛打分（批 13，共享词表淹没个人区分度） |
| `config/scoring.yaml` | 全部打分行为开关：粗筛等权/tie-break、ranker 六维权重与定级阈值（含 push_floor 推送下限、batch_size 精排批处理大小）、journal_channel 顶刊直采通道开关（enabled/tiers/max_per_user/retmax_per_journal）、learned 学习护栏（含 negative_factor_weak/strong，详见第 4、9 节） |
| `config/journals.yaml` | 期刊分层 T0（CNS 正刊+顶级子刊 22 本）/ T1（综合强刊、基因组、进化生态、昆虫领域强刊）；仅用于精排 journal 维度与周报统计 |
| `config/email.yaml` | daily_paper_number 15 / show_translation / show_keywords / show_doi（`feedback_email`、`feedback_base_url`、`feedback_secret` 非 yaml 键，由 main.py 运行时从 `.env` 的 DIGEST_FROM_EMAIL / FEEDBACK_BASE_URL / FEEDBACK_SECRET 注入） |
| `prompts/` | 10 个 prompt 文件：paper_analysis / paper_news_summary / paper_translation / daily_value_summary / recommendation_reason / recommendation_reason_batch（精排批处理，批 11）/ feedback_term_extraction / term_expansion / topic_clustering / weekly_report |
| `run_daily.sh` / `run_weekly.sh` / `run_monthly.sh` | cron 入口脚本（日报/周报/月报），均前置节假日判断（当日节假日记日志跳过；日报节后经 `--days` 合并补发）；run_daily.sh 另会先确保 feedback/server.py 反馈服务已在后台运行（pgrep 检查，未运行则 nohup 拉起，缺 FEEDBACK_SECRET 时进程自行退出不影响主流水线）；日志 `logs/pipeline.log`、`logs/cron.log`、`logs/feedback_server.log` |
| `tests/` | 237 个测试全 mock 无网络依赖，`.venv/bin/python -m pytest tests/ -q` |

---

## 4. 评分体系（三层漏斗）

### 第 1 层：检索召回（sources/）
**主路径（全局摄取层 `global_pool.py`）**：全用户检索词（aliases 展开 + 有效学习词）合并去重 →
LLM 主题聚类分簇（按词表哈希缓存 7 天）→ 逐簇一条扁平 OR PubMed 查询
（簇内全部关键词等权、命中任一词即召回，无 AND、不按物种分组、无降级重试）合并去重
\+ bioRxiv 按日期全量拉取后全局词表扁平 OR 过滤
\+ **顶刊直采通道（`top_journals.py`，批 7）**：按 journals.yaml 指定分层刊名逐刊 `"<刊名>"[jour]`
直抓 PubMed 最近论文并入池中——绕过关键词召回，解决顶刊与实验室词表弱相关时进不了候选池的问题
（scoring.yaml `journal_channel.enabled` 开关，`tiers`/`retmax_per_journal` 可调）
→ 当日全局池；exclude 不进全局检索（各用户粗筛时各自剔除）。
**遗留逐人路径（`pubmed.py`，不在主流水线，仅测试/调试）**：严格查询（物种 OR）AND（其余词 OR），
命中不足 5 篇降级为全词扁平 OR；exclude 词在查询端 NOT 排除。
两源合并按 DOI/标题去重（PubMed 已发表版优先）。

### 第 2 层：规则粗筛（`scorer.py` + `config/scoring.yaml`）
零成本关键词匹配，把零相关论文挡在昂贵的 LLM 处理之外：

| 规则 | 分值 | 说明 |
|---|---|---|
| 检索词命中 | +1/词 | 四组个人词表（species/methods/research_interest/keywords）全部等权；原词或其 aliases/自动扩展变体任一命中即计，多变体不重复计；lab_topics 不参与粗筛（批 13），只在全局召回与精排 lab 维度生效 |
| 标题命中 | +1 | 变体出现在标题（title_bonus） |
| 命中频次 | +1/次 | 按命中最多的变体计，封顶 3 次（frequency_bonus/frequency_cap） |
| 学习词命中 | +有效权重 | 单篇封顶 6 分（learned.score_cap，不压过手配词表） |
| exclude 命中 | 剔除 | 直接淘汰 |

粗筛只负责排序选出 top-N（默认 15）候选，**不定级、不含期刊因素**。
顶刊通道论文关键词得分常为 0，由 main.py 在 top-N 之后按刊名补入候选
（每用户每日上限 `journal_channel.max_per_user`，默认 10），是否推送交由精排决定。

### 第 3 层：个性化精排（`ranker.py`）
六维各 0-100，按权重加权为 Final Score（0-100）：

| 维度 | 权重 | 计算方式 |
|---|---|---|
| journal 期刊影响力 | 30 | journals.yaml 分层：T0=100 / T1=70 / 未分层=30（刊名规范化后精确匹配） |
| personal 个人相关度 | 30 | LLM 依据个人画像语义判断（异常回退 50；2026-07-29 起原 recency 的 10 分并入本维度） |
| lab 实验室方向 | 20 | lab_topics 命中 0/1/2/3/≥4 个 → 0/25/50/75/100（批 13 梯度化，防大词表下 ≥2 即饱和） |
| method 方法相关度 | 10 | 个人 methods 命中 0/1/≥2 个 → 0/50/100 |
| novelty 新颖性 | 10 | LLM 依据 AI 分析判断（异常回退 50） |
| recency 时效性 | 0 | 当天~1 天=100 / 2 天=80 / 3 天=60 / 周内=40 / 更早=20 / 日期无法解析=50（权重归零，维度仍计算，调回即可恢复） |

**定级**：Final Score ≥75 → Must Read；≥60 → Important；其余 Reference。
绝对阈值、宁缺毋滥——当日全部低分则可以没有 Must Read，不按固定配额凑数（V4 替代旧配额定级）。
**推送下限（批 7）**：Final Score < `thresholds.push_floor`（默认 30）的论文不进邮件——
不相关小刊论文被挡掉，不相关 T0 顶刊（journal 30 分）仍可以 Reference 露面
（2026-07-29 随 recency 权重归零从 40 下调：顶刊原 journal 30 + recency 10 = 40，现仅 30）；
删掉该行则恢复全量推送。
**合并封顶**：关键词通道 top-N 与顶刊通道补入候选合并精排后，按下限过滤再按 Final Score
降序截断到 `--limit`（默认 15）——两通道凭分数竞争，CNS 高分自然挤掉关键词通道低分论文。
同时 LLM 生成一句中文推荐理由展示在卡片上。
**批处理与并发（批 11）**：LLM 相关度/新颖性/理由判断按 `ranker.batch_size`（默认 5）篇合为
一次调用（输出 JSON 数组逐篇解析；整批非法或数量不符时整批回退中性分 50，单条非法仅该条回退），
批次间按 model.yaml `max_workers`（默认 8）并发——精排 LLM 调用量约为篇数 ÷ batch_size，
15 篇约 3 次。

---

## 5. 每日流水线（`run_daily.sh` → 节假日闸门 → `main.py`）

```
DAYS=$(.venv/bin/python -m scheduler.holiday)   # 节假日闸门：当日在 holidays.yaml 返回 0
# DAYS=0 → 记日志退出（反馈收集一并跳过，节后首个工作日处理，无损失）；
# 否则 DAYS=1+当日之前连续节假日天数（上限 10，节后合并补发覆盖空窗）
python -m feedback                              # 先收集回信并学习（见第 9 节）
python main.py --days "$DAYS"                   # 再三阶段执行：

阶段一 · 逐人词表准备（对每个 active 用户）
  1. 加载实验室公共方向（apply_lab_profile：lab_topics + aliases 并入配置副本）
  2. 加载反馈学习词表（vocab.load_active_terms，半衰期衰减后 ≥0.3 才生效）
  3. 加载/刷新自动词表 auto_terms（扩展词+反馈新增词并入配置副本，见第 8 节）

阶段二 · 全局摄取一次（sources/global_pool.py）
  4. 全用户词表合并去重 → LLM 主题聚类分簇（7 天缓存）
  5. 逐簇扁平 OR PubMed 检索 + bioRxiv 全局过滤 → 合并去重成当日全局池
     （journal_channel.enabled 时并入顶刊直采通道：按 journals.yaml 刊名逐刊直抓）

阶段三 · 逐用户分发
  6. 每用户本地规则粗筛等权打分（仅个人词表，lab_topics 批 13 起剥离；期刊不参与）
  7. 跨天去重：该用户历史已发论文（recommendations 表）跳过；取 top-N（默认 15）；
     顶刊通道论文按刊名补入候选（每用户上限 journal_channel.max_per_user，默认 10）
  8. 各用户 shortlist 求并集，并集只做一次 LLM 处理
     （结构化分析 → 新闻摘要 → 中文四段摘要；SQLite 全局缓存复用，同篇全实验室只算一次；
     缓存未命中论文按 model.yaml max_workers 并发调 LLM）
  9. 每用户六维精排 + 生成推荐理由（LLM 判断按 ranker.batch_size 篇一批、批次间并发）
     → 按绝对阈值定级 → push_floor 推送下限过滤
     → 按 Final Score 截断封顶 --limit（默认 15，关键词通道与顶刊通道凭分数竞争）；
     过滤后为空则当日跳过发信，宁缺毋滥
  10. 入库 SQLite（论文/分析/摘要/翻译全局共享，推荐记录按用户隔离）
  11. 生成今日价值总结 → 渲染总览块+四段式 HTML → SMTP 发送
     主题：Daily Literature Intelligence Report · <日期>
     dry-run：不发信不写库，HTML 写 logs/digest_<日期>_<用户>.html
```

单用户任何环节异常只记录日志并继续下一个用户，不中断整体；任一用户失败 `main()` 返回 1
（cron 可感知）。LLM 日预算耗尽时快速失败，不发空壳邮件。

## 6. 周报/月报流水线（`run_weekly.sh` / `run_monthly.sh` → `weekly_report.py`）

```
weekly_report.py --days 7         # 周报（run_weekly.sh，cron 每周一，前置节假日判断）
weekly_report.py --days 30        # 月报（run_monthly.sh，cron 每月 1 日，前置节假日判断）
  （--user 可选，--dry-run 不落邮件；当日节假日记日志跳过，窗口自然滚动覆盖）
  聚合 recommendations ⋈ papers ⋈ news 窗口内记录
  → compute_stats：定级分布 / 期刊分层 / Top 期刊 / 高频关键词
  → compute_reading_trends：窗口内反馈正/中/负分桶（新旧反馈值归一化兼容）
    + 当前有效学习词 Top（半衰期衰减后权重）
  → LLM 趋势总结（仅基于 Must Read / Important）
  → 四段式周报/月报邮件
```

---

## 7. 邮件规格

**日报（开头总览块 + 三段式卡片）**：总览块（窗口内全库新增 X 篇 · 命中您的关键词 Y 篇 ·
本次推送 Z 篇，附必读/重要/参考定级分布）；Part 1 今日论文——每篇一张卡片，
默认可见区为定级徽标 + Final Score + 标题链接（附中文标题）+ 一句话新闻概要
（问题→方法→创新→结果）+ 期刊日期；卡片内 `<details>` 折叠「展开详情」
（作者、DOI、链接、Keywords、AI 推荐理由、中文四段结构化摘要——背景/研究方法/
研究结果/研究意义，`show_translation` 开启时只展示中文、不再显示英文摘要，关闭回退英文摘要），
不支持 `<details>` 的邮件客户端默认全部展开、可读性不受影响；⭐ 重要性反馈在折叠区外，
不展开即可点击；Part 2 今日价值总结；Part 3 反馈入口
（HTTP 模式为"新增关注关键词"，回退模式为批量标注 mailto，见下）。

**周报/月报（四段式）**：Part 1 LLM 趋势总结；Part 2 分布统计（定级/期刊分层/Top 期刊/高频词）；
Part 3 阅读趋势（窗口内反馈正/中/负分桶 + 当前有效学习词 Top）；Part 4 重点论文清单
（周报/月报清单不带反馈入口，反馈入口只在日报）。

**反馈 · HTTP 模式（默认，批 6 起）**：`.env` 配置 `FEEDBACK_BASE_URL` + `FEEDBACK_SECRET` 后，
日报每张卡片底部内嵌 **⭐1–⭐5 五个链接**（⭐1 完全不相关 / ⭐2 不太相关 / ⭐3 一般 /
⭐4 比较重要 / ⭐5 非常重要），接收人点一下即完成反馈——链接指向 `feedback/server.py` 的
`/fb` 端点（`u=邮箱&p=paper_id&v=星级&t=HMAC token`），落库 feedback 表（幂等去重）后浏览器
直接显示"✓ 已反馈"确认页（附论文标题与新增关键词表单），**无需再发任何邮件**；
Part 3 收窄为单个"新增关注关键词"按钮（`/kw` 表单，提交写入 auto_terms feedback_added，
次日进检索）。token 覆盖 用户|论文|星级 全参数，篡改即 403，无 FEEDBACK_SECRET 无法伪造
（防污染他人学习词表）；服务随 `run_daily.sh` 自动拉起（pgrep 检查 + nohup）。

**反馈 · 回退模式（未配置 FEEDBACK_*）**：日报 Part 3 单个 mailto 按钮，主题
`[FB] u=<用户邮箱> d=<日期>`；正文预填当天全部编号行（`01:`–`NN:`，编号与卡片展示序一致），
在想评的编号后填 1-5，其余留空即可；collector 按编号经 recommendations 表
（user_email + sent_date，按分数展示序）映射回 paper_id。正文其余行可自由填写原因（可选）；
另起一行以 `+` 开头可新增检索词（如 `+CRISPR, 单细胞测序`，逗号兼容中英文），校验发件人后
追加进该用户 auto_terms 的 feedback_added，次日进检索（见第 9 节）。
旧逐篇格式 `[FB] u=<邮箱> p=<id> v=<1..5>` 的回信 collector 仍兼容收集。

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

## 9. 反馈学习闭环（全自动，五星制）

1. 反馈采集（双通道，同一 feedback 表幂等汇合）：
   - **HTTP 模式（默认）**：用户在日报卡片底部直接点 ⭐1–⭐5 → `feedback/server.py` 校验
     HMAC token → 写 feedback 表 → 浏览器显示"已反馈"确认页（附新增关键词表单，
     提交经 `/kw` 写入 auto_terms 的 `feedback_added`，次日进检索）；全程无需发邮件；
   - **回退模式**：用户在日报末尾 Part 3 点 **一键反馈** → 邮件客户端生成 `[FB]` 回信草稿
     （主题带 `d=<日期>`，正文预填当天全部编号行），在想评的编号后填 ⭐1–⭐5 发出；
     正文可填原因（非必填），另起一行 `+关键词` 可新增检索词。
2. 每日流水线**之前** `python -m feedback`：IMAP 收取未读 `[FB]` 回信 →
   **校验发件人 From 与主题标注用户一致**（不符拒绝记录，防伪造污染他人词表）→
   批量格式按编号经 recommendations 表映射回 paper_id（越界编号跳过并告警）→
   写 feedback 表（幂等：同人同篇同评级去重）→ 标记已读；
   校验通过后正文 `+关键词` 行经清洗去重追加到该用户 auto_terms 的 `feedback_added`，次日进检索。
   随后执行 `learner.learn_from_feedback`（HTTP 通道写入的未处理行同样在此进入学习），按星级四档映射：
   - ⭐4 / ⭐5（正）：LLM 从论文提炼候选新词；同一词需 **≥2 篇**高分论文支持才提权
     （初始 1.0，每多一篇 +0.5，上限 3.0）——防漂移护栏；
   - ⭐3（中性）：只记录，不调整；
   - ⭐2（弱负）：**只**对该篇命中的学习词降权（×negative_factor_weak=0.5），
     不动手配词表、不写 exclude；
   - ⭐1（强负）：命中的学习词 ×negative_factor_strong=0.25；同一（用户, 词）累计第 2 次
     强负时在审计日志追加 `exclude_candidate` 记录（人工排查线索，不自动入 exclude）。
3. 学习词参与次日检索与粗筛：读取时按 30 天半衰期衰减，有效权重 <0.3 视为失效；
   全部调整写审计日志 `logs/feedback_learning.log`（JSONL，可回滚）。

历史兼容：旧四值（relevant / not_relevant / already_read / save）回信不再产生，
collector 按非法值忽略；周报阅读趋势统计通过 `normalize_feedback_value`
把历史行归一化到正/中/负三桶，新旧数据可同窗统计。

护栏参数全部在 `config/scoring.yaml` 的 `learned` 节（含 negative_factor_weak/strong），
调行为只改配置。

---

## 10. 运维速查

```bash
cd ~/lab-literature-inteligence

.venv/bin/python -m pytest tests/ -q        # 全量测试（当前 237 passed）
.venv/bin/python main.py --dry-run          # 全用户试跑：不发信，HTML 落 logs/
.venv/bin/python main.py --user user001 --days 3   # 单用户调试，回溯 3 天
.venv/bin/python weekly_report.py --dry-run        # 周报预览
.venv/bin/python weekly_report.py --days 30 --dry-run   # 月报预览
.venv/bin/python -m feedback --learn-only          # 跳过 IMAP 只重跑学习
.venv/bin/python -m feedback.server                # 启动五星反馈服务（0.0.0.0:8710，run_daily.sh 会自动拉起）
.venv/bin/python -m scheduler.holiday              # 打印今日回溯天数（0=节假日跳过）
tail -f logs/pipeline.log                          # 流水线日志
tail -f logs/feedback_server.log                   # 反馈服务访问日志
```

crontab 示例（日报每天、周报每周一、月报每月 1 日；脚本均内置节假日闸门）：

```cron
30 7 * * *   cd ~/lab-literature-inteligence && ./run_daily.sh >> logs/cron.log 2>&1
53 7 * * 1   cd ~/lab-literature-inteligence && ./run_weekly.sh >> logs/cron.log 2>&1
26 7 1 * *   cd ~/lab-literature-inteligence && ./run_monthly.sh >> logs/cron.log 2>&1
```

- 新增用户：复制 `config/users/user001.yaml` 改 name/email/词表即可，无需改代码；
  次日自动获得 LLM 扩展词缓存。
- 调推荐行为：只改 `config/scoring.yaml`（粗筛/精排权重、定级阈值、学习护栏）、
  `config/journals.yaml`（期刊分层）、`config/email.yaml`（篇数与展示开关），不改代码。
- 节假日表：每年初按国务院放假安排手工维护 `config/holidays.yaml` 一次。
- 换模型：改 `config/model.yaml` + `.env` 的 `OPENAI_API_KEY` / `OPENAI_BASE_URL`。
- 五星反馈服务：`.env` 配 `FEEDBACK_BASE_URL`（接收人设备可访问本机的地址，查本机局域网 IP：
  `ipconfig getifaddr en0`，如 `http://192.168.1.10:8710`）与 `FEEDBACK_SECRET`
  （`openssl rand -hex 32` 生成）；不配则邮件自动回退为批量回信标注模式，两通道可同时工作。
- 密钥全部在 `.env`（SMTP/IMAP/OPENAI/FEEDBACK_SECRET），已 gitignore；`.env.example` 为模板。

---

## 11. 待办与已知问题

来源：`docs/OPTIMIZATION_REPORT.md`（全量审查，基准 commit `b3b701c`，即 V4 改造前）。
V4 已完成：等权粗筛、期刊因素移入精排、绝对阈值定级替代配额定级、
journal_fallback 低相关兜底退役、LLM 自动扩展词每日缓存。

| # | 事项 | 优先级 | 状态 |
|---|---|---|---|
| 1 | ~~反馈收集不校验发件人 From，任何人可伪造 `[FB]` 回信污染他人学习词表（中危安全漏洞，`feedback/collector.py`）~~ | P0 | **已修**（2026-07-28）：collect() 校验发件人与主题标注用户一致，不符拒绝记录 |
| 2 | ~~LLM 产物（分析/新闻/翻译）按用户重复计算，全局表只写不读；50 用户时成本/时长放大 5–10 倍~~ | P0 | **已修**（2026-07-28，批 2）：全局摄取层一次检索 + `processing/artifacts.py` 产物层——shortlist 并集只算一次，SQLite 全局缓存逐篇写回复用 |
| 3 | ~~LLMClient 无 max_tokens/超时/429 退避/日预算护栏~~ | P0 | **已修**（2026-07-28）：timeout/max_tokens/退避重试/日预算全部落地，参数在 `config/model.yaml`；增量持久化子项随批 2 闭环（产物逐篇写回 SQLite，中途失败下次运行读缓存续跑） |
| 4 | ~~`main()` 恒返回 0，流水线失败 cron 无感知（静默失败）~~ | P0 | **已修**（2026-07-28）：`main.py` 与 `weekly_report.py` 任一用户失败返回 1 |
| 5 | 关键词为子串匹配无词边界（如 "ant" 误中 "plant"）；learned 词入库未净化 | P1 | 待修 |
| 6 | 无 users 表（email 字符串为身份键）；SQLite 未开 WAL/busy_timeout、无 schema 版本 | P1 | 待修 |
| 7 | 召回纯关键词、无语义检索层（无 embedding）；新用户冷启动依赖手写词表 | P1 | 待修（语义通道+RRF 融合；批 2 的 LLM 主题聚类分簇检索已部分缓解召回盲区，批 7 顶刊直采通道解决顶刊漏召回） |
| 8 | 死结构：~~`scheduler/` 空目录~~、`schedule` 死依赖、`.env.example` 的 `OPENAI_MODEL` 死配置、依赖未钉版本 | P1 | 部分已清（2026-07-28，批 3）：`scheduler/` 已由节假日模块启用；其余三项待清理 |
| 9 | 反馈深度不足：`reason` 文本未分析、`already_read` 未作弱信号、排序权重静态不学习 | P2 | 待演进 |
| 10 | 数据源仅 PubMed+bioRxiv；N/C/S RSS、arXiv 未接入；测试全 mock 无集成测试 | P2 | 待演进 |

---

## 12. 总体行动方案与下一步（2026-07-28 四项决策全部确认）

### 12.1 决策落地规格

**决策 1 · 反馈通道 = 邮件回信（路径 A）**：~~部署机器不能被其他用户设备访问，
不引入任何本地 HTTP 服务，零入站端口；反馈继续走 `[FB]` mailto 回信 + IMAP 收集。~~
**（2026-07-29 用户撤销）**：为实现"点星即反馈、无需再发邮件"，引入本地 HTTP 反馈服务
（批 6，`feedback/server.py`）。原约束缓解方式：服务仅监听一个端口、所有写操作要求
HMAC token（密钥在 `.env`，不入库不入文档）、参数强校验、页面输出全转义；
未配置 `FEEDBACK_BASE_URL`/`FEEDBACK_SECRET` 时整体回退为零入站端口的回信模式。
安全攻击面：SMTP/IMAP/OPENAI/FEEDBACK_SECRET 凭据（`.env` + gitignore）、
From 伪造校验（回信通道）与 HMAC token 校验（HTTP 通道）。

**决策 2 · 五星反馈**（替代 relevant / not_relevant / already_read / save 四键）：
日报末尾 Part 3 一键反馈——单个 mailto 按钮生成一封回信，主题 `[FB] u=<邮箱> d=<日期>`，
正文预填当天编号行，按编号填 ⭐1–⭐5（批 5 起由逐篇 5 链接改为批量一封；
旧逐篇 `p=<id> v=<1..5>` 格式 collector 仍兼容收集）；
正文 `+关键词1, 关键词2` 语法保留（写入 `auto_terms` 的 `feedback_added`，次日进检索）。
星级→学习信号映射（最小改动复用现有学习逻辑，参数进 `config/scoring.yaml` learned 节）：

| 星级 | 语义 | 学习行为 |
|---|---|---|
| ⭐5 / ⭐4 | 正（≈旧 relevant/save） | LLM 提炼新词，沿用 ≥2 篇支持才提权护栏（初始 1.0 / +0.5 / 上限 3.0） |
| ⭐3 | 中性（≈旧 already_read） | 只记录，不调整 |
| ⭐2 | 负 | 该篇命中的学习词 ×0.5 |
| ⭐1 | 强负 | 该篇命中的学习词 ×0.25，审计标注；同词累计 2 次 ⭐1 进人工排除候选 |

**决策 3 · 精排六维权重**：journal 30 / personal 30 / lab 20 / method 10 / novelty 10 / recency 0
（合计 100，结构不变只改 `config/scoring.yaml` ranker.weights，并对齐 `ranker.py` 默认值）。
口径说明：2026-07-28 按六维数值落地（`ed34425` + `fbd8b2c`）时
分组标签"规则 40（lab+method）/ LLM 20（personal+novelty）"与六维数值存在 10 分差异；
2026-07-29 用户决策把 recency 的 10 分并入 personal（personal 20→30、recency 10→0，
维度仍计算、权重归零），现为规则 60（journal 30+lab 20+method 10）/ LLM 40（personal 30+novelty 10）。
副作用与处理：不相关 T0 顶刊得分 40→30，`thresholds.push_floor` 同步 40→30 以保住"CNS 露面"。

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
| 批 1 P0 修复 | #1 collector 防伪造、#3 LLMClient 护栏、#4 失败返回非 0 | ✅ 已完成（2026-07-28，`5113b0d`） |
| 批 2 全局池 | 全局词表合并 → LLM 主题聚类（7 天缓存）→ 每簇一条 PubMed 查询合并全局池 → 按人本地粗筛分发；LLM 产物按并集 shortlist 只算一次并 SQLite 缓存复用（解决第 11 节 #2、#3 剩余子项） | ✅ 已完成（2026-07-28，`5113b0d`） |
| 批 3 产品功能 | B6 中文四段结构化摘要（`fbc94ef`）→ B7 邮件开头总览（同 `fbc94ef`）→ B1 权重落地（`ed34425`+`fbd8b2c`）→ B5 节假日静态表（`aa5074d`）→ B3 月报·30 天阅读趋势+领域总结（`0d161e9`）→ B2+B4 五星反馈与关键词回信语法（`6b61269`） | ✅ 已完成（2026-07-28，195 passed） |
| 批 5 检索与反馈 | 检索改扁平 OR：簇内全部关键词等权、命中任一词即召回，去掉"物种 AND 其余词"分组与严格/宽松降级重试（`global_pool.py` / `biorxiv.py` 全局路径；`pubmed.py` 逐人路径留作遗留不在主流水线）；反馈合并为日报末尾 Part 4 一封批量邮件（编号填 ⭐1–⭐5，编号经 recommendations 展示序映射；旧逐篇格式兼容收集） | ✅ 已完成（2026-07-29，207 passed） |
| 批 6 邮件内五星反馈 | 撤销决策 1 零入站端口约束，引入 HTTP 反馈服务：日报每张卡片内嵌 ⭐1–⭐5 链接（`feedback/server.py` /fb 点星即落库、浏览器显示"已反馈"页+关键词表单；/kw 独立关键词入口；`feedback/tokens.py` HMAC 签名防伪造）；未配置 FEEDBACK_BASE_URL/FEEDBACK_SECRET 回退批 5 批量回信；服务随 run_daily.sh 自动拉起 | ✅ 已完成（2026-07-29，219 passed） |
| 批 7 召回与推送质量 | 顶刊直采通道（`sources/top_journals.py`：按 journals.yaml 分层刊名 `"<刊名>"[jour]` 逐刊直抓 PubMed 并入全局池，绕过关键词召回解决全局池零 CNS；`global_pool.fetch_global_pool` 新增 journal_channel 参数；main.py top-N 后按刊名补入通道候选，每用户上限 max_per_user=10）+ 推送下限（`ranker.thresholds.push_floor`：低分论文不进邮件，过滤后为空则当日跳过发信）；配置均在 scoring.yaml `journal_channel` / `ranker.thresholds` | ✅ 已完成（2026-07-29，225 passed） |
| 批 8 精排权重调整 | recency 权重 10→0 并入 personal（20→30）：journal 30 / personal 30 / lab 20 / method 10 / novelty 10 / recency 0；push_floor 随顶刊得分变化 40→30（不相关 T0 顶刊现仅 journal 30 分，保持露面机会）；recency 维度仍计算，配置调回即恢复 | ✅ 已完成（2026-07-29） |
| 批 9 bioRxiv 窗口修复 | bioRxiv 按日期区间全量拉取，抓取窗口向前多覆盖一天，修复预印本时间分布导致新文长期捞不到、推送零 bioRxiv（`855728a`） | ✅ 已完成（2026-07-29，225 passed） |
| 批 10 日报卡片合并 | Part 1 新闻摘要表与 Part 2 详情卡片合并：每篇一张卡片，默认可见区为徽标/得分/标题链接/一句话新闻概要/期刊日期，`<details>` 折叠作者、DOI、关键词、推荐理由与中文四段摘要（不支持的客户端默认全展开）；⭐ 反馈留在折叠区外不展开即可点；价值总结与反馈入口顺延为 Part 2 / Part 3 | ✅ 已完成（2026-07-29，227 passed） |
| 批 11 性能改造 | 并发 + 批处理 + token 计量：① `artifacts.py` 产物生成线程池并发（model.yaml `max_workers=8`，worker 只调 LLM，SQLite 读写留主线程）；② `ranker.py` 精排批处理（scoring.yaml `ranker.batch_size=5`，`judge_batch` 一次调用评判多篇、批次间并发，整批非法/数量不符整批回退中性分、单条非法仅该条回退，prompt `recommendation_reason_batch.txt`）；③ `llm.py` 用量文件累计 prompt/completion/total tokens（类级锁保护并发读写），`main.py` 结束打 `get_usage()` 用量日志 | ✅ 已完成（2026-07-29，237 passed） |
| 批 12 词表拓展与召回贯通 | ① lab.yaml 按 55 个课题方向拓展：31 → 203 topics（LLM 生成候选 → PubMed 90 天命中逐词验证，数据 `logs/lab_term_verify.json`；d90>250 的过宽词与零命中词有意不收，防 lab 维度饱和与全局池灌水）；② `global_pool.collect_global_terms` 把 lab_topics 并入全局检索词（此前只参与打分，实验室方向文献可能漏召回）；③ 个人词表修复：user001 补 GWAS/RNAi/DNA language model 等、user002 methods 补单细胞分析短词 + 新增 keywords、user003 methods 长短语死词替换为可匹配短词 + 新增 keywords | ✅ 已完成（2026-07-30） |
| 批 13 修复共享词表污染 | 批 12 副作用：203 个全员共享 lab_topics 等权计入粗筛，个人词区分度被淹没、各用户候选趋同。① `scorer.py` 粗筛打分剥离 lab_topics（配置残留该权重也硬跳过），lab_topics 只在全局召回与精排 lab 维度生效；② `ranker.lab_relevance` 改四档梯度（命中 0/1/2/3/≥4 → 0/25/50/75/100），防大词表下 ≥2 即满分饱和普涨总分；③ `scoring.yaml` weights 删除 lab_topics 行 | ✅ 已完成（2026-07-30，238 passed） |
| 批 14 lab.yaml 按示例文献重构 | 用户反馈"词太细、筛出大量不符文献"。批 12 的 203 词版本此前已被裁剪（物种专名分组已删），生效版本残留的裸方法词/裸概念词（synteny、centromere、transposable element、裸 sex determination、Tissue architecture、Cellular niches 等）单独命中海量非演化论文。按 `lab_literature.exmple.txt` 整体重构为"演化框架 × 组学方法"短语词表：87 topics（动物起源与多细胞化/细胞类型演化/单细胞图谱/空间组学/社会性昆虫/比较基因组学/调控演化/染色体演化/性染色体/脑演化/AI 基础模型 11 组）+ 17 条 aliases（英美拼写、连字符变体、单复数，命中不重复计分）。候选词 PubMed 90 天命中验证（`logs/lab_term_verify_v2.json`）；示例文献标题回归：85 个真实标题 77 个 ≥1 命中（放过 8 篇裸 AI/单细胞方法学，有意不收），6 条无关负例 0 命中 | ✅ 已完成（2026-07-30，238 passed） |

### 12.3 批 4 候选（第 11 节 P1/P2 余项）

#5 关键词词边界匹配 + learned 词入库净化；#6 users 表 + SQLite WAL/busy_timeout + schema 版本；
#7 embedding 语义召回通道 + RRF 融合（解决冷启动与同义词漏召回）；#8 死结构清理
（`schedule` 死依赖、`.env.example` 死配置、依赖钉版本，`scheduler/` 由 B5 启用）；
#9 反馈深度（reason 文本分析、⭐3 弱信号、排序权重学习）；#10 数据源扩展（arXiv / NCS RSS）与集成测试。
