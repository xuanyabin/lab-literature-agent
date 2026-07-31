# 项目状态（截至 2026-07-31）

Lab Literature Intelligence System：面向多用户的个性化文献情报平台。
PROJECT.md 规划的 Phase 0–6 已全部完成，V4 粗筛改造进行中（已完成：去期刊化 + 等权 +
阈值定级 + LLM 扩展词召回），测试 120 passed。

## 当前架构

### 每日流水线（`main.py`，GitHub Actions `.github/workflows/daily.yml` 触发）

调度：每天 UTC `30 23 * * *`（北京时间 07:30，对齐原本地 cron），支持 workflow_dispatch 手动触发。
运行状态托管：`scripts/pull_state.sh` 运行前从孤儿 `state` 分支还原
`literature_agent.db`、`config/users/auto_terms/`、`logs/.llm_usage.json`，
结束后 `scripts/push_state.sh` 以单 commit 强推回 state 分支（历史不累积）；
`feedback_data/`（用户反馈小文件）每日提交回 main。本地 `run_daily.sh` 保留为手动备用。

```
python -m feedback          # 先跑：IMAP 收集反馈回信 → 学习闭环更新 learned 词表
python main.py              # 再跑：对每个 active 用户（config/users/*.yaml）依次执行
  ├─ 加载 learned 词表（feedback/vocab.py，与手配词表分离）
  ├─ 加载自动词表（config/users/auto_terms/<slug>.yaml：LLM 扩展词仅用于召回、等权，自动刷新）
  ├─ PubMed 检索（严格/宽松降级）+ bioRxiv 按日期拉全量本地过滤 → 合并去重
  ├─ 规则粗筛打分（V5 分层：个人词 + lab_recall = default_groups 全员组 + 订阅 topic_groups，全部等权；
  │   rank_only 高噪音词不打粗筛分；noise_terms 医学噪音软惩罚减分；个人词标题强命中兜底补入）
  ├─ 按用户跨天去重（recommendations 表）
  ├─ AI 处理：摘要分析 → 一句话科研新闻 → 中文翻译（LLMClient，config/model.yaml）
  ├─ 个性化精排（六维加权 Final Score + AI 推荐理由 → 按绝对阈值定级 Must Read/Important/Reference）
  ├─ 入库 SQLite（papers / paper_analysis / paper_news_summary / recommendations）
  └─ 每日价值总结 → 三段式 HTML 邮件（卡片 ⭐1-5 一键点选 webhook 直写 / mailto 回信降级）
```

### 每周流水线（`weekly_report.py`，GitHub Actions `.github/workflows/weekly.yml` 触发）

调度：每周日 UTC `53 23 * * 0`（北京时间周一 07:53）；月报为 `.github/workflows/monthly.yml`，
每月 1 日 UTC `26 0 1 * *`（北京时间 08:26，比本地原时间晚 1 小时以错开月初调度高峰），
跑 `weekly_report.py --days 30`。周/月报不学习反馈（无 feedback 步骤与 feedback_data 回流），
但同样经 pull_state / push_state 读写 state 分支（推荐记录写 db）。

```
对每个 active 用户：聚合 SQLite 最近 7 天推荐记录（不重新检索分析）
  ├─ 分布统计（定级 / 期刊分层 / Top 期刊 / 高频关键词，纯数据）
  ├─ LLM 周度趋势总结（仅基于 Must Read / Important 的一句话新闻）
  └─ 三段式 HTML 周报邮件
```

### 模块划分

| 目录 | 职责 |
|---|---|
| `sources/` | PubMed、bioRxiv 采集，统一输出 `Paper` 结构 |
| `processing/` | LLM 封装、摘要分析、新闻生成、翻译、日/周总结、词表扩展 |
| `recommendation/` | 粗筛打分（scorer）+ 六维精排（ranker） |
| `database/` | SQLite 持久化（papers、recommendations、feedback、learned_terms 等） |
| `mailer/` | 日/周 HTML 组装（digest_builder、weekly_builder）+ SMTP 发送 |
| `feedback/` | 反馈文件队列（store）、IMAP 收集、学习闭环（提权/降权/30 天半衰期衰减） |
| `worker/` | Cloudflare Worker：星标一键反馈 webhook（HMAC 校验 → GitHub API 直写 `feedback_data/pending/`） |
| `config/` | 配置驱动：lab / model / scoring / journals / email / users/*.yaml |
| `prompts/`、`templates/` | Prompt 与邮件模板独立文件，禁硬编码 |

## 已完成模块

- [x] Phase 0 项目初始化
- [x] Phase 1 单用户 MVP（PubMed → AI 分析 → 新闻生成 → 邮件）
- [x] Phase 1.5 三段式日报（新闻摘要 / 详细卡片 / 每日价值总结）
- [x] Phase 2 SQLite 持久化
- [x] Phase 3 多用户系统（实验室公共方向叠加个人词表，按用户隔离去重）
- [x] Phase 4 个性化推荐引擎（六维加权 Final Score + AI 推荐理由）
- [x] Phase 5 反馈学习系统（⭐ 一键点选（Worker 直写 feedback_data）/ 回信降级标注 → 收集 → 全自动词表学习，无人工审核）
- [x] Phase 5.5 bioRxiv 预印本数据源 + 高水平期刊加权与低相关兜底
- [x] Phase 6 每周情报报告（个性化周报：趋势总结 + 分布统计 + 重点清单）

当前接入用户：user001、user002（新增用户只需加 `config/users/xxx.yaml`）。

## 已知问题

- 数据源仅 PubMed + bioRxiv；Nature/Cell/Science RSS、arXiv 在 PROJECT.md 2.3 中列为未来扩展，尚未接入
- bioRxiv 无服务端检索，按日期拉全量后本地过滤，请求体积大（有模块级缓存，多用户共享一次抓取）
- 日报强依赖 LLM（分析/翻译/推荐理由/总结）；LLM 接口不可用时主流程会失败。周报趋势段已做容错（失败置空并显示兜底文案）
- 逐篇星标配了 webhook（`FEEDBACK_WEBHOOK_URL`/`FEEDBACK_SECRET`，Cloudflare Worker 直写仓库）
  则不依赖邮箱；mailto 降级与 Part 3 批量标注仍依赖 `.env` 的 IMAP 配置，未配置时该通道标注无法回收
- 周报趋势总结只读一句话新闻（控制 token），不参考原始摘要，深度有限
- 交付与反馈唯一通道是邮件，无 Web 端查看入口
- SQLite 单文件存储，不支持多机部署或并发写
- 定时调度已迁移 GitHub Actions（仓库需 Private）：运行状态靠 state 分支单 commit 强推托管，
  强推丢失风险存在（无历史）；SMTP 587 出站在 GitHub runner 一般可用，若被拦截需改用 API 发信

## 下一步计划

- 接入更多数据源：Nature / Cell / Science RSS、arXiv（PROJECT.md 2.3 既定扩展方向）
- 运营观察：连续跑几周日报+周报，根据真实反馈调 `config/scoring.yaml` 权重与期刊分层
- 扩大用户规模：向 50+ 成员目标推进，验证 bioRxiv 缓存与 LLM 成本在多用户下的表现
- 视反馈情况考虑：Web 端历史报告查看、反馈维度细化（如"已读"参与降权之外的学习信号）
