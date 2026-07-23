# Lab Literature Intelligence System

面向 50+ 成员科研团队的多用户个性化文献情报平台。系统每日自动获取生命科学领域最新论文（PubMed / bioRxiv），经 AI 摘要理解与一句话科研新闻生成后，按实验室公共方向与成员个人兴趣个性化排序，发送每日文献推荐邮件，并通过用户反馈持续优化推荐。

完整开发与治理规范见 [PROJECT.md](PROJECT.md)。

## 目录结构

```
├── main.py            # 入口（Phase 1 实现）
├── config/            # 配置驱动：lab.yaml / model.yaml / scoring.yaml / users/*.yaml
├── sources/           # 文献采集（PubMed、bioRxiv）
├── processing/        # 摘要理解 + 科研新闻生成
├── recommendation/    # 个性化排序
├── database/          # SQLite 持久化
├── mailer/            # HTML 邮件生成与发送
├── feedback/          # 用户反馈收集与推荐优化
├── prompts/           # 所有 Prompt 独立 txt 文件
├── templates/         # 邮件模板
├── scheduler/         # 每日任务调度
├── logs/
└── tests/
```

> 注：规范中邮件模块目录名为 `email/`，因与 Python 标准库 `email` 同名会遮蔽
> `smtplib` 所依赖的标准库模块，经确认更名为 `mailer/`。

## 环境搭建

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入 OPENAI_API_KEY 与 SMTP 配置
```

## 配置驱动原则

- 新增用户：仅新增 `config/users/xxx.yaml`
- 修改实验室方向：仅修改 `config/lab.yaml`
- 切换模型：仅修改 `config/model.yaml`
- 调整 Prompt：仅修改 `prompts/*.txt`
- 调整推荐权重：仅修改 `config/scoring.yaml`
- 调整期刊分层：仅修改 `config/journals.yaml`
- API Key / SMTP 密码：仅存放于 `.env`（已 gitignore），禁止写入代码

## 开发阶段

- [x] Phase 0 项目初始化
- [x] Phase 1 单用户 MVP（PubMed 获取 → AI 分析 → 新闻生成 → 邮件发送）
- [x] Phase 1.5 三段式邮件结构（新闻摘要 / 详细卡片 / 每日价值总结，配置见 config/email.yaml）
- [x] Phase 2 数据库持久化
- [x] Phase 3 多用户系统（遍历 config/users/ 所有 active 用户，实验室公共方向叠加个人词表，按用户隔离的跨天去重）
- [x] Phase 4 个性化推荐引擎（六维加权 Final Score：个人相关度/实验室方向/期刊影响力/新颖性/方法相关度/时效性，附 AI 推荐理由）
- [x] Phase 5 反馈学习系统（邮件卡片反馈链接 → IMAP 收集标注 → 全自动学习词表：提权/降权/衰减，作用于检索与粗筛打分）
- [ ] Phase 6 每周情报报告

## 反馈学习（Phase 5）

每日邮件的论文卡片底部带 4 个反馈链接（相关 / 不相关 / 已读 / 收藏），点击生成回信草稿，
发送即完成标注。每日主流程前运行收集与学习：

```bash
python -m feedback            # IMAP 收集回信 + 学习闭环
python -m feedback --learn-only   # 只学习已收集的反馈（不连邮箱）
```

- 需在 `.env` 配置 `IMAP_HOST`（发件邮箱开启 IMAP；`IMAP_USER`/`IMAP_PASSWORD` 缺省回退 SMTP 配置）
- 高分标注（相关/收藏）→ LLM 提炼新检索词，同一词在 ≥2 篇高分论文出现才提权生效；
  低分标注（不相关）只对命中的学习词降权，不触碰手配词表
- 学习词按半衰期 30 天衰减，作用于检索查询与粗筛打分；全部变更写 `logs/feedback_learning.log` 审计
- 学习参数见 `config/scoring.yaml` 的 `learned` 节

## 工程规范

- 所有文件操作限制在项目目录内
- 按 Phase 开发，每阶段测试通过后 commit（格式 `type: description`）
- 禁止在 Python 中硬编码用户信息或 Prompt
