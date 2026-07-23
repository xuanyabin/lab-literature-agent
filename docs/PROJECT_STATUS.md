# 项目状态（截至 2026-07-23，commit `b3b701c`）

Lab Literature Intelligence System：面向多用户的个性化文献情报平台。
PROJECT.md 规划的 Phase 0–6 已全部完成，测试 115 passed。

## 当前架构

### 每日流水线（`main.py`，cron 经 `run_daily.sh` 触发）

```
python -m feedback          # 先跑：IMAP 收集反馈回信 → 学习闭环更新 learned 词表
python main.py              # 再跑：对每个 active 用户（config/users/*.yaml）依次执行
  ├─ 加载 learned 词表（feedback/vocab.py，与手配词表分离）
  ├─ PubMed 检索（严格/宽松降级）+ bioRxiv 按日期拉全量本地过滤 → 合并去重
  ├─ 规则粗筛打分（个人词 + 实验室公共方向 + 别名扩展 + 期刊分层加分 T0+8/T1+3）
  ├─ 按用户跨天去重（recommendations 表）→ journal_fallback 低相关兜底
  ├─ AI 处理：摘要分析 → 一句话科研新闻 → 中文翻译（LLMClient，config/model.yaml）
  ├─ 个性化精排（六维加权 Final Score + AI 推荐理由 → Must Read/Important/Reference）
  ├─ 入库 SQLite（papers / paper_analysis / paper_news_summary / recommendations）
  └─ 每日价值总结 → 三段式 HTML 邮件（卡片带反馈链接，回信即标注）
```

### 每周流水线（`weekly_report.py`，cron 经 `run_weekly.sh` 触发，建议周一）

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
| `feedback/` | IMAP 收集、学习闭环（提权/降权/30 天半衰期衰减） |
| `config/` | 配置驱动：lab / model / scoring / journals / email / users/*.yaml |
| `prompts/`、`templates/` | Prompt 与邮件模板独立文件，禁硬编码 |

## 已完成模块

- [x] Phase 0 项目初始化
- [x] Phase 1 单用户 MVP（PubMed → AI 分析 → 新闻生成 → 邮件）
- [x] Phase 1.5 三段式日报（新闻摘要 / 详细卡片 / 每日价值总结）
- [x] Phase 2 SQLite 持久化
- [x] Phase 3 多用户系统（实验室公共方向叠加个人词表，按用户隔离去重）
- [x] Phase 4 个性化推荐引擎（六维加权 Final Score + AI 推荐理由）
- [x] Phase 5 反馈学习系统（邮件链接标注 → IMAP 收集 → 全自动词表学习，无人工审核）
- [x] Phase 5.5 bioRxiv 预印本数据源 + 高水平期刊加权与低相关兜底
- [x] Phase 6 每周情报报告（个性化周报：趋势总结 + 分布统计 + 重点清单）

当前接入用户：user001、user002（新增用户只需加 `config/users/xxx.yaml`）。

## 已知问题

- 数据源仅 PubMed + bioRxiv；Nature/Cell/Science RSS、arXiv 在 PROJECT.md 2.3 中列为未来扩展，尚未接入
- bioRxiv 无服务端检索，按日期拉全量后本地过滤，请求体积大（有模块级缓存，多用户共享一次抓取）
- 日报强依赖 LLM（分析/翻译/推荐理由/总结）；LLM 接口不可用时主流程会失败。周报趋势段已做容错（失败置空并显示兜底文案）
- 反馈闭环依赖 `.env` 的 IMAP 配置；未配置时标注无法回收，学习不生效
- 周报趋势总结只读一句话新闻（控制 token），不参考原始摘要，深度有限
- 交付与反馈唯一通道是邮件，无 Web 端查看入口
- SQLite 单文件存储，不支持多机部署或并发写
- 日报/周报 cron 需手动 `crontab -e` 安装，仓库只提供 `run_daily.sh` / `run_weekly.sh`

## 下一步计划

- 接入更多数据源：Nature / Cell / Science RSS、arXiv（PROJECT.md 2.3 既定扩展方向）
- 运营观察：连续跑几周日报+周报，根据真实反馈调 `config/scoring.yaml` 权重与期刊分层
- 扩大用户规模：向 50+ 成员目标推进，验证 bioRxiv 缓存与 LLM 成本在多用户下的表现
- 视反馈情况考虑：Web 端历史报告查看、反馈维度细化（如"已读"参与降权之外的学习信号）
