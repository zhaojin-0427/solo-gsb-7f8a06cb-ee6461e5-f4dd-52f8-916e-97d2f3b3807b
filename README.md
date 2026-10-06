# 冻结文件交换（A/B 双节点）

两个小组分别保管审查材料，需要互换冻结文件。系统由两个独立节点组成：
各自持有独立 SQLite 持久卷，各自提供页面；文件提交即冻结（内容与 SHA-256 摘要不可修改），
双方提交后由 A 生成不可撤销决议并驱动交换，回执丢失、并发重试、节点重启都能收敛到
同一决议和每方一份正确文件。

技术栈：Python · FastAPI · SQLite（WAL + BEGIN IMMEDIATE 事务）· httpx · 原生 JavaScript。

## 启动（单条命令）

```bash
docker compose up --build
```

## 两端端口

| 节点 | 页面地址 | 说明 |
| --- | --- | --- |
| A | http://localhost:8001 | 创建交换、A 组参与页面 |
| B | http://localhost:8002 | B 组参与页面（凭 A 分发的链接访问） |

两个服务共用同一镜像，以 `NODE_ROLE=A/B` 区分角色；数据分别持久化在命名卷
`a-data`、`b-data`（容器内 `/data/node.db`），互不影响。

## 基本流程

1. 打开 http://localhost:8001 ，点击「创建新交换」。A 生成两条随机参与链接：
   A 方链接（8001 端口）与 B 方链接（8002 端口），把 B 方链接分发给 B 组。
2. 双方各自打开自己的链接，选择 **1～64 KiB** 的文件提交：
   - 提交后内容与 SHA-256 摘要不可修改；
   - 重复提交相同内容返回原回执（幂等）；
   - 提交不同内容返回 **409**。
3. 双方都提交后，A 自动从 B 取得 B 的已冻结摘要，**先持久化含双方摘要的不可撤销决议**，
   再把决议和 A 文件发给 B；B 在事务中核对本地 B 摘要与收到的 A 文件摘要，
   保存 A 文件和决议后返回回执（含 B 文件及摘要）；A 核验并持久化 B 文件后才标记完成。
4. 页面实时显示状态与摘要：
   - 决议前任何一端都**不能下载**对方文件（403）；
   - 决议后若对端不可达，显示「**待同步**」而非完成；
   - 只有收到并核对对方文件的节点，才在本方链接开放下载。
5. 两端后台任务持续重试未确认的决议（A 重发决议、B 重推回执），
   节点重启后自动恢复；页面上的「立即重试同步」可手动触发，并发重试同样安全。

## 演练：B 丢失回执（首次持久化决议后返回 503）

1. 把 `docker-compose.yml` 中 `node-b` 的 `DRILL_FAIL_FIRST_RESOLUTION` 改为 `"1"`，
   然后 `docker compose up -d --build`。
2. 创建交换，双方提交文件。B 在事务中持久化决议与 A 文件后，按演练开关返回 503
   （模拟“事务已提交但回执丢失”）：
   ```bash
   docker compose logs -f node-b   # 可看到 [drill] ... 决议已持久化，按演练开关返回 503
   ```
3. 此时 A 端页面显示「待同步」；A 的后台任务自动重试，B 发现决议已存在且内容一致，
   直接重返回执——双方最终形成同一决议、每方一份正确文件（B 侧 `drill_fired` 标记
   保证每个交换只演练一次）。
4. 配合重启演练：在交换进行中执行 `docker compose restart node-a` 或
   `docker compose restart node-b`，两端恢复后自动重试未确认的决议，最终仍然收敛。
5. 演练完毕把开关改回 `"0"` 并 `docker compose up -d`。

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt

# 终端 1（A）
NODE_ROLE=A PEER_URL=http://127.0.0.1:18002 NODE_SECRET=testsecret \
  PUBLIC_BASE=http://127.0.0.1:18001 PEER_PUBLIC_BASE=http://127.0.0.1:18002 \
  DB_PATH=/tmp/fx-a/node.db uvicorn app.main:app --port 18001

# 终端 2（B，开启演练开关）
NODE_ROLE=B PEER_URL=http://127.0.0.1:18001 NODE_SECRET=testsecret \
  PUBLIC_BASE=http://127.0.0.1:18002 PEER_PUBLIC_BASE=http://127.0.0.1:18001 \
  DB_PATH=/tmp/fx-b/node.db DRILL_FAIL_FIRST_RESOLUTION=1 \
  uvicorn app.main:app --port 18002

# 终端 3：冒烟测试（提交约束、幂等、409、下载限制、并发重放、收敛）
python3 scripts/smoke_test.py
```

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `NODE_ROLE` | `A` | 节点角色：`A` 创建并驱动交换，`B` 接受注册与决议 |
| `PEER_URL` | — | 对端节点的集群内地址 |
| `NODE_SECRET` | `dev-secret` | 节点间共享密钥（`X-Node-Key` 头），两端必须一致 |
| `PUBLIC_BASE` / `PEER_PUBLIC_BASE` | — | 本端/对端对外地址，用于生成参与链接 |
| `DRILL_FAIL_FIRST_RESOLUTION` | `0` | 仅 B：置 `1` 后每个交换首次持久化决议返回 503 |
| `WORKER_INTERVAL` | `2` | 后台重试间隔（秒） |
| `DB_PATH` | `/data/node.db` | SQLite 文件路径 |

## 一致性设计要点

- **先持久化，后发送**：A 先把决议（双方摘要 + 决议时间）写入本地事务，再发给 B；
  重试只能重发同一份已持久化决议。
- **事务内核对**：B 在同一事务中核对本地 B 摘要、A 文件摘要，保存 A 文件与决议；
  重复消息内容一致则重返回执，不一致则 409 拒绝。
- **核验后才完成**：A 校验回执摘要等于已持久化决议中的 B 摘要，持久化 B 文件后才
  标记完成；B 主动推送回执直到 A 确认，两条路径均幂等。
- **写序列化**：所有写操作走 `BEGIN IMMEDIATE` 事务，A 的并发重试在 B 侧被串行化，
  最终只形成同一决议和每方一份正确文件。

## 重置数据

```bash
docker compose down -v   # 删除两个持久卷，清空全部交换
```
