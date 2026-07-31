"""反馈文件队列（feedback_data/）：IMAP 收集到的反馈先落盘为 YAML，学习后按月归档。

目录结构：
    feedback_data/
      pending/              # 待学习队列（一个文件一条反馈）
      processed/YYYY-MM/    # 已学习归档（按反馈时间分月，控制单目录规模）

pending 文件名：u<用户邮箱 sha256 前 8 位>_p<论文id>_v<星级>.yaml——同名即同一条反馈
（对应 feedback 表 UNIQUE(user, paper, value) 语义），重复收取幂等跳过；文件名
不含明文邮箱，yaml 内容保留明文 user_email（学习闭环按用户分组需要）。
该目录随 git 跟踪流转（无服务器部署下反馈经提交同步），数据量小、可审计。
"""

import hashlib
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_BASE_DIR = BASE_DIR / "feedback_data"


def _filename(user_email: str, paper_id: int, value: str) -> str:
    digest = hashlib.sha256(user_email.encode("utf-8")).hexdigest()[:8]
    return f"u{digest}_p{paper_id}_v{value}.yaml"


def save_pending(feedback: dict, base_dir: Path = DEFAULT_BASE_DIR) -> Path | None:
    """把一条反馈写入 pending 队列，返回文件路径；同名文件已存在（仍在待学习
    或已归档）则幂等跳过返回 None。feedback 需含 user_email / paper_id / value，
    可带 reason / source（一并落盘保留）。"""
    base_dir = Path(base_dir)
    name = _filename(feedback["user_email"], feedback["paper_id"], str(feedback["value"]))
    path = base_dir / "pending" / name
    if path.exists() or any(base_dir.glob(f"processed/*/{name}")):
        return None
    record = {
        "user_email": feedback["user_email"],
        "paper_id": feedback["paper_id"],
        "value": str(feedback["value"]),
        "reason": feedback.get("reason") or "",
        "source": feedback.get("source") or "email",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(record, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    return path


def load_pending(base_dir: Path = DEFAULT_BASE_DIR) -> list[dict]:
    """读取全部待学习反馈（按文件名排序保证顺序稳定），每条附带自身 path（供
    学后 mark_processed 归档）；损坏文件记日志后跳过（留在原地人工排查）。"""
    entries = []
    for path in sorted((Path(base_dir) / "pending").glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            logger.warning("反馈文件损坏，跳过：%s", path)
            continue
        entries.append({**data, "path": path})
    return entries


def mark_processed(path: Path, base_dir: Path = DEFAULT_BASE_DIR) -> Path:
    """把 pending 文件移到 processed/YYYY-MM/（月份取文件内 timestamp，缺失或
    损坏时回退当前月），返回归档后的路径。"""
    path = Path(path)
    month = ""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        ts = str(data.get("timestamp", ""))
        if re.match(r"\d{4}-\d{2}", ts):
            month = ts[:7]
    except (OSError, yaml.YAMLError):
        pass
    if not month:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
    dest_dir = Path(base_dir) / "processed" / month
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    shutil.move(str(path), dest)
    return dest
