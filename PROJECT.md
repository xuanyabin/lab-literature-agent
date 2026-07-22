Lab Literature Intelligence System V3.1
Development Specification & Engineering Governance
0. Project Mission（项目目标）

你将协助我开发一个长期运行的科研文献智能情报系统。

这不是一个临时脚本，而是一个面向实验室长期使用的：

Multi-user Personalized Literature Intelligence Platform

系统部署在我的 Mac 本地环境。

最终服务对象：

一个拥有 50+ 成员的科研团队。

系统目标：

每天自动获取全球生命科学领域最新论文，根据：

实验室公共研究方向；
每个成员个人研究兴趣；
关键词；
方法偏好；
历史反馈行为；

生成个性化文献推荐邮件。

核心目标：

帮助科研人员每天快速判断：

哪些论文值得阅读。

1. 核心工作流程（System Workflow）

系统整体流程：

Literature Sources

        ↓

Paper Collection

        ↓

Metadata Cleaning

        ↓

Abstract Understanding AI

        ↓

Research News Generator ⭐

        ↓

Personalized Ranking

        ↓

Daily Email Digest

        ↓

User Feedback

        ↓

Recommendation Optimization

2. 功能需求（Functional Requirements）
2.1 用户规模

系统必须支持：

50+ 用户

每个用户拥有独立配置：

包括：

用户姓名
邮箱
研究方向
兴趣关键词
技术关键词
物种关键词
排除方向

例如：

name:
Zhang


email:
xxx@example.com


research_interest:

- insect evolution
- single cell
- brain evolution


keywords:

- honeybee
- glia
- snRNA-seq


methods:

- single-cell RNA sequencing


species:

- Apis
- Bombus

2.2 配置驱动原则（非常重要）

系统必须采用：

Configuration Driven Architecture

禁止：

把用户信息写入 Python。

错误：

users=[
{
"name":"xxx"
}
]

正确：

用户全部来自：

config/users/

新增成员：

只需要：

config/users/user003.yaml

无需修改：

Python代码
数据库结构
推荐算法
2.3 文献来源

第一阶段支持：

PubMed
bioRxiv

未来扩展：

Nature RSS
Cell RSS
Science RSS
arXiv
2.4 文献输入范围

系统只需要分析：

Title
Abstract
Keywords
Authors
Journal
Publication Date
DOI
URL

不需要：

PDF下载
全文解析
Figure解析
Supplementary解析
2.5 每日推荐任务

每天自动执行：

获取最新论文

↓

去重

↓

AI理解摘要

↓

生成科研新闻摘要

↓

用户个性化排序

↓

每人生成15篇

↓

发送邮件

3. 邮件设计（Daily Digest）

邮件目标：

让用户30秒内判断：

今天有没有值得阅读的论文。

第一部分：Research News Digest

每天15篇论文首先展示：

类似科研新闻。

每篇：

论文标题

一句话新闻摘要

期刊

发表时间

推荐等级

一句话新闻摘要生成规范 ⭐

这是系统核心模块。

长度：

50-80中文字符。

必须包含：

科学问题/痛点

+

作者采用的方法

+

核心发现


格式：

一句话总结：

由于XXX问题尚未解决，
作者利用XXX方法，
发现XXX机制/规律，
推动XXX领域研究。

禁止：

❌ “本文研究了……”

❌ 简单重复标题

❌ 空泛评价

❌ 编造摘要不存在的信息

目标：

让科研人员10秒判断论文价值。

示例

错误：

本研究利用单细胞技术研究昆虫。

正确：

为解决不同昆虫脑细胞类型缺乏统一比较体系的问题，作者整合多物种单细胞数据建立细胞图谱，揭示神经细胞类型保守性与谱系特异扩张规律。

第二部分：Paper Card

每篇论文包含：

基础信息
英文标题
中文标题
作者
Journal
Publication Date
DOI
文献访问链接
Abstract

展示原始摘要。

Keywords
AI推荐理由

说明：

为什么推荐给该用户。

推荐等级

四类：

Must Read

Important

Reference

Ignore

每日发送：

15篇：

建议：

Must Read

3篇


Important

5篇


Reference

7篇


Ignore只作为内部评分。

4. 个性化推荐系统
4.1 Lab Profile

实验室公共兴趣。

文件：

config/lab.yaml

例如：

topics:

- animal evolution
- developmental biology
- genomics
- single-cell
- spatial transcriptomics
- pangenomics
- AI biology

4.2 User Profile

目录：

config/users/

每个人独立yaml。

4.3 推荐评分模型

每篇论文针对每个用户计算：

Final Score =


35%
Personal relevance


+

25%
Lab relevance


+

15%
Journal influence


+

10%
Novelty


+

10%
Method relevance


+

5%
Recency


输出：

{
score:95,

category:"Must Read"

}

5. AI模块设计
Module 1
Literature Collector

目录：

sources/

负责：

PubMed API
bioRxiv API

输出：

统一Paper对象。

格式：

{
"title":"",
"abstract":"",
"authors":"",
"journal":"",
"date":"",
"doi":"",
"url":""

}

Module 2
Abstract Understanding Engine

输入：

Title

Abstract

Keywords

输出：

{
"field":"",
"problem":"",
"solution":"",
"finding":"",
"methods":[],
"organisms":[]

}


注意：

只能基于摘要。

禁止幻觉。

Module 3 ⭐
Research News Generator

目录：

processing/news_generator.py

负责生成：

一句话科研新闻摘要。

Prompt独立保存。

Module 4
Recommendation Engine

目录：

recommendation/

负责：

用户排序。

Module 5
Email Generator

目录：

email/

生成HTML邮件。

Module 6
Feedback System

目录：

feedback/

收集：

用户行为。

包括：

★★★★★

Relevant

Not Relevant

Already Read

Save


原因：

研究方向相关

方法有价值

数据资源重要

推荐错误

6. 数据库设计

第一版：

SQLite。

数据库：

literature_agent.db
users
id

name

email

active

user_profiles
user_id

research_interest

keywords

methods

species

lab_profile
topic

keywords

weight

papers
id

title

abstract

authors

journal

date

doi

url

paper_analysis
paper_id

problem

solution

finding

methods

organisms

paper_news_summary ⭐
paper_id

summary

created_time

paper_scores
paper_id

user_id

score

category

recommendations
user_id

paper_id

date

sent

feedback
user_id

paper_id

rating

reason

timestamp

7. Prompt管理规范

所有Prompt必须独立文件。

目录：

prompts/

paper_analysis.txt

news_summary.txt

recommendation_reason.txt

weekly_report.txt


禁止：

Python代码中直接写Prompt。

8. 模型配置

默认模型：

GPT-5.4

配置：

config/model.yaml

例如：

provider:
openai


model:
gpt-5.4


temperature:
0.2

API Key规则

禁止：

代码中出现：

OPENAI_API_KEY="xxxx"

必须：

.env

例如：

OPENAI_API_KEY=

OPENAI_MODEL=gpt-5.4


如果缺少：

API Key
SMTP配置
邮箱密码
用户信息

必须：

停止开发并询问。

禁止自行假设。

9. 项目目录结构

最终：

lab-literature-intelligence/


├── main.py


├── config/

│
├── users/

│
├── sources/

│
├── processing/

│   ├── analyzer.py
│   ├── news_generator.py
│
├── recommendation/

│
├── database/

│
├── email/

│
├── feedback/

│
├── prompts/

│
├── templates/

│
├── scheduler/

│
├── logs/

│
└── tests/

10. 工程治理规范
10.1 Git必须使用

初始化：

git init

每个阶段完成：

必须：

测试通过

↓

git commit

Commit格式：

type: description

例如：

chore: initialize project

feat: add pubmed collector

feat: implement news generator

fix: resolve email issue

10.2 文件操作限制

所有操作必须限制在：

当前项目目录。

禁止：

修改：

Desktop

Documents

系统文件

其它项目


执行任何：

创建

修改

删除

必须确认路径属于项目目录。

10.3 代码维护要求

系统必须满足：

新增用户：

只增加yaml。

修改兴趣：

只修改yaml。

修改模型：

只修改model.yaml。

修改Prompt：

只修改txt。

调整推荐：

只修改scoring.yaml。

11. 开发行动计划

严格按照以下阶段执行。

Phase 0
项目初始化

完成：

检查当前工作目录
创建项目结构
Python虚拟环境
requirements.txt
README
.gitignore
Git初始化

完成后：

commit：

chore: initialize project structure
Phase 1
单用户MVP

实现：

PubMed获取
Paper对象
AI摘要分析
News Generator
Email发送

commit：

feat: implement single user literature pipeline
Phase 2
数据库

加入：

papers
analysis
summaries

commit：

feat: add database persistence
Phase 3
多用户系统

加入：

users
profiles
lab profile

commit：

feat: implement multi user recommendation
Phase 4
推荐系统

加入：

scoring
ranking
classification

commit：

feat: add personalized ranking engine
Phase 5
Feedback系统

加入：

用户反馈
推荐优化

commit：

feat: add feedback learning system
Phase 6
Weekly Report

实现：

每周总结邮件。

commit：

feat: implement weekly intelligence report
12. Codex执行规则（必须遵守）

你不是在写一次性脚本。

你是在开发长期运行的实验室基础设施。

必须：

不一次生成全部代码；
按Phase开发；
每个Phase完成测试；
测试通过后commit；
修改前说明计划；
显示修改文件列表；
保持模块化；
保持配置驱动；
遇到不确定需求暂停询问；
不自行生成关键账号信息。
当前任务

现在开始：

Phase 0：项目初始化

请执行：

检查当前工作目录；
确认不会修改其它目录；
创建项目结构；
创建Python虚拟环境；
创建requirements.txt；
创建README；
创建.gitignore；
初始化Git；
第一次commit。

如果任何步骤需要：

API Key
SMTP账号
用户信息
外部服务配置

立即停止并询问。

不要自行假设。

End of Specification
