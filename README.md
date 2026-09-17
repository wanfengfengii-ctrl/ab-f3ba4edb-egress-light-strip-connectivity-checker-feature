# 地下通道灯带连通性验收服务

纯后端验收服务（Python 3.12 · FastAPI · Pydantic）：在地下通道封板前，校验接线栅格中
**唯一电源 `P`** 发出的电流能否经导线 `W` 送达每一盏出口灯 `E`。空位 `X` 隔断电流；
只有共享上、下、左、右四条边的单元才连通——**对角相触不导电，越界不连通**。
服务自行实现从 `P` 出发的广度优先图搜索，不依赖任何图算法库。

验收结果只有两种：

- **`PASS`** —— 全部出口灯可达，可以封板；
- **`FAIL`** —— 列出每个不可达出口的编号，以及该出口所在不可达连通分量中
  按行号、列号升序最先的单元坐标（行、列均从 0 开始）作为证据。
  失败出口按编号的 Unicode 码点升序输出。

## 目录结构

```
app/
  main.py          FastAPI 入口、路由与结构化错误处理（create_app 工厂）
  models.py        Pydantic 请求/响应/错误模型与排空状态契约
  validation.py    输入契约校验（整图拒绝的全部规则）
  connectivity.py  四邻接 BFS 图搜索
  inspector.py     可达性判定与证据坐标计算
  drain.py         排空协调器（接纳/排空中/已排空状态机 + 在途计数）与检查准入中间件
tests/             pytest：连通算法边界判据 + 输入校验 + 端到端 API + 排空生命周期
acceptance.py      一次性验收脚本（verify 服务使用；末尾的排空检查会排空实例）
Dockerfile         python:3.12-slim 镜像（同时打包测试与 docker-compose.yml，供 verify 容器内校验）
docker-compose.yml api 服务 + verify 一次性验收服务
```

## API

### `POST /inspect`

请求体为 JSON 对象，含两个同尺寸矩阵：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `grid` | `string[][]` | 1–200 行 × 1–200 列，行宽一致；单元只能是 `P` / `W` / `E` / `X`，且 `P` 恰好一个 |
| `labels` | `(string\|null)[][]` | 与 `grid` 同尺寸；每个 `E` 位置填**唯一非空字符串**编号，其余位置必须为 `null` |

下列任一情况整图拒绝（HTTP 422，结构化错误，见下文）：行宽不齐、尺寸越界、
字符未知、电源数量非一、编号缺失、编号错位（非 `E` 位置出现编号）、编号为空、编号重复。

#### 示例：全部可达 → `PASS`

```bash
curl -s -X POST http://localhost:8000/inspect \
  -H 'Content-Type: application/json' \
  -d '{
    "grid": [
      ["P", "W", "E"],
      ["X", "X", "W"],
      ["E", "W", "W"]
    ],
    "labels": [
      [null, null, "EXIT-01"],
      [null, null, null],
      ["EXIT-02", null, null]
    ]
  }'
```

```json
{"result": "PASS"}
```

#### 示例：存在不可达出口 → `FAIL`

```bash
curl -s -X POST http://localhost:8000/inspect \
  -H 'Content-Type: application/json' \
  -d '{
    "grid": [
      ["P", "W", "W", "X", "W", "E"],
      ["X", "X", "W", "X", "X", "X"],
      ["W", "W", "W", "X", "E", "X"]
    ],
    "labels": [
      [null, null, null, null, null, "EXIT-B"],
      [null, null, null, null, null, null],
      [null, null, null, null, "EXIT-A", null]
    ]
  }'
```

```json
{
  "result": "FAIL",
  "failures": [
    {"exit_id": "EXIT-A", "evidence": {"row": 2, "col": 4}},
    {"exit_id": "EXIT-B", "evidence": {"row": 0, "col": 4}}
  ]
}
```

注意 `EXIT-B`：出口本体在 `(0, 5)`，但其不可达连通分量是 `{(0, 4), (0, 5)}`，
按行号、列号升序最先的单元是导线 `(0, 4)`——证据坐标指向分量首单元，而非出口本身。

#### 示例：输入非法 → `422` 结构化错误

```bash
curl -s -X POST http://localhost:8000/inspect \
  -H 'Content-Type: application/json' \
  -d '{"grid": [["P", "W", "P"]], "labels": [[null, null, null]]}'
```

```json
{
  "detail": {
    "code": "INPUT_REJECTED",
    "message": "The diagram violates the input contract; see issues for every problem found.",
    "issues": [
      {
        "code": "POWER_SOURCE_COUNT",
        "message": "Expected exactly one power source 'P', found 2.",
        "locations": [{"row": 0, "col": 0}, {"row": 0, "col": 2}]
      }
    ]
  }
}
```

错误码一览（`detail.issues[].code`）：

| code | 含义 |
| --- | --- |
| `GRID_EMPTY` / `GRID_TOO_TALL` | 行数为 0 或超过 200 |
| `ROW_EMPTY` / `ROW_TOO_WIDE` | 存在空行或行宽超过 200 |
| `GRID_RAGGED` | 行宽不齐 |
| `LABELS_ROW_COUNT_MISMATCH` / `LABELS_ROW_LENGTH_MISMATCH` | 编号矩阵与栅格尺寸不符 |
| `UNKNOWN_CELL` | 出现 `P`/`W`/`E`/`X` 之外的字符 |
| `POWER_SOURCE_COUNT` | 电源 `P` 数量非一 |
| `EXIT_ID_MISSING` / `EXIT_ID_EMPTY` | `E` 位置编号缺失或为空字符串 |
| `EXIT_ID_MISPLACED` | 非 `E` 位置出现编号 |
| `EXIT_ID_DUPLICATE` | 编号重复 |
| `SCHEMA_ERROR` | 请求体不符合 JSON 结构（类型错误、缺字段、多字段、非法 JSON 等） |

### `POST /drain`

滚动更新前的优雅排空。调用后服务沿 `ACCEPTING → DRAINING → DRAINED`
单向转换：立即停止接纳新的 `POST /inspect`，并等待**此前已准入**的检查全部
结束（成功、422 拒绝、内部异常、请求取消都会在 finally 路径释放在途计数，
排空不会永久等待），随后返回：

```bash
curl -s -X POST http://localhost:8000/drain
```

```json
{"state": "DRAINED"}
```

- **并发幂等**：并发调用 `/drain` 复用同一次转换，所有调用在在途计数归零后
  得到完全相同的响应；排空后再次调用立即返回同一结果。
- **转换后到达的检查**收到结构化 `503`，`detail.state` 携带当前状态
  （`DRAINING` 或 `DRAINED`）：

```json
{
  "detail": {
    "code": "SERVICE_DRAINING",
    "message": "The service is draining for a rolling update: new inspections are rejected while previously admitted ones finish.",
    "state": "DRAINING"
  }
}
```

- **终态**：`DRAINED` 不可撤销，只有进程重启才恢复接纳（无反排空接口）。
- 排空前已准入的请求不受干扰，仍按原有 `PASS`、`FAIL` 或 `422` 完成；
  不调用 `/drain` 时，健康检查、端口配置与验收流程完全不变。

### `GET /healthz`

健康检查，返回 `{"status": "ok"}`。交互式 API 文档见 `/docs`。

## 运行

### 本地（Python 3.12）

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Docker Compose（只运行 API）

```bash
docker compose up --build          # 宿主 8000 端口
API_PORT=9000 docker compose up    # 用 API_PORT 覆盖宿主端口
```

### 一次性验收（verify 服务）

`verify` 与 `api` 共享同一镜像——镜像由 `api` 服务唯一构建（`verify` 只引用、
不重复构建，避免同名镜像在并行构建时冲突），并通过 `pull_policy: never`
保证不会误从镜像仓库拉取同名镜像。`verify` 等待 API 健康后，先跑完整
pytest 套件，再对运行中的 API 执行 `acceptance.py` 的实网 HTTP 验收
（PASS/FAIL/排序/证据坐标/各类 422），随后退出并以退出码报告结果：

```bash
docker compose --profile verify up --build --abort-on-container-exit --exit-code-from verify
```

也可对任意已运行的实例单独执行验收脚本（注意：脚本末尾的排空检查会
调用 `/drain` 永久排空该实例，重跑前需重启 API 进程）：

```bash
API_BASE_URL=http://localhost:8000 python acceptance.py
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

测试覆盖连通算法的边界判据：单单元栅格、1×N 条带、200×200 满栅格、
对角不导通、空位隔断、网格边缘不回绕（Python 负索引陷阱）、
证据坐标取分量首单元、同分量多出口共享证据、Unicode 码点排序，
以及全部输入拒绝规则与端到端 PASS/FAIL 报文。

排空生命周期（tests/test_drain.py）用可控阻塞的检查请求证明：
排空会等待已准入的旧请求、以结构化 503 拒绝新请求，并在成功、
校验拒绝、内部异常与请求取消四条 finally 路径后解除等待；
并发排空调用共享同一次转换并返回相同结果；不调用排空时普通检查
报文无任何回归。
