"""反馈学习系统（Phase 5）：收集用户标注 → 全自动学习闭环 → 学习词表生效。

- collector：IMAP 轮询发件邮箱，解析 "[FB]" 回信写入 feedback 表；
- server：HTTP 五星反馈接收服务（/fb 点星即记录、/kw 新增关键词表单，
  HMAC token 防伪；python -m feedback.server，随 run_daily.sh 自动拉起）；
- tokens：反馈链接的 HMAC 签名/校验（digest_builder 与 server 共用）；
- learner：高分标注 → LLM 提词 → 防漂移提权；低分标注 → 学习词降权；
  全部变更写审计日志 logs/feedback_learning.log；
- vocab：学习词表读取侧，时间衰减 + 失效过滤，供检索与打分使用。

用法：
    python -m feedback            # 收集回信 + 对所有 active 用户执行学习闭环
    python -m feedback --learn-only   # 跳过 IMAP 收集，只处理库中未学习反馈
"""
