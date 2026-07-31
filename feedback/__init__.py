"""反馈学习系统（Phase 5）：收集用户标注 → 全自动学习闭环 → 学习词表生效。

- collector：IMAP 轮询发件邮箱，解析 "[FB]" 回信（逐篇 ⭐1-5 mailto 与批量编号
  标注两种格式），双写 feedback_data/pending 文件队列与 feedback 表；
- store：feedback_data/ 文件队列（pending 待学习 → processed/YYYY-MM 已学习归档，
  文件名幂等去重）；
- learner：读 pending 队列学习——高分标注 → LLM 提词 → 防漂移提权；低分标注 →
  学习词降权；学后归档文件并把 feedback 表对应行标 processed=1；
  全部变更写审计日志 logs/feedback_learning.log；
- vocab：学习词表读取侧，时间衰减 + 失效过滤，供检索与打分使用。

用法：
    python -m feedback            # 收集回信 + 对所有 active 用户执行学习闭环
    python -m feedback --learn-only   # 跳过 IMAP 收集，只学习 pending 队列中的反馈
"""
