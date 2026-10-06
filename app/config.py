"""节点配置：全部通过环境变量注入，A/B 共用同一镜像。"""
from __future__ import annotations

import os

# 节点角色：A 创建交换并驱动协议；B 接受注册、核对决议。
ROLE = os.environ.get("NODE_ROLE", "A").upper()

# 对端节点的集群内地址（节点间 REST 调用）。
PEER_URL = os.environ.get("PEER_URL", "http://localhost:8002").rstrip("/")

# 节点间共享密钥（X-Node-Key 头）。
NODE_SECRET = os.environ.get("NODE_SECRET", "dev-secret")

# 本节点 / 对端节点对外可访问的 base URL（用于生成参与链接）。
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "http://localhost:8001").rstrip("/")
PEER_PUBLIC_BASE = os.environ.get("PEER_PUBLIC_BASE", "http://localhost:8002").rstrip("/")

# 演练开关（仅 B 生效）：开启后，B 在每个交换首次持久化决议之后返回 503，
# 模拟“事务已提交但回执丢失”。
DRILL_FAIL_FIRST_RESOLUTION = os.environ.get("DRILL_FAIL_FIRST_RESOLUTION", "0") == "1"

# 冻结文件大小限制：1 字节 ~ 64 KiB。
MIN_FILE_SIZE = 1
MAX_FILE_SIZE = 64 * 1024

# 后台重试间隔（秒）。
WORKER_INTERVAL = float(os.environ.get("WORKER_INTERVAL", "2"))
