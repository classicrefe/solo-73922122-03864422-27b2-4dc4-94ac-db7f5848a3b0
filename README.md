# 采样-检测记录配对 / 质控放行服务

实验室数据工程师与质控员使用的两个接口：

1. `POST /match` —— 把**人工采样登记**与**仪器检测记录**对上号，只把所有同等最优方案中
   都成立的配对列为确定配对，绝不任选一套最优方案冒充确定结论；
2. `POST /quality-release` —— 质控员从同一检测项目的**上机序列**（质控针 + 样本针）中
   选出可入分析库的样本读数，按质控窗口与复测链规则放行或扣留。

---

## 一、`POST /quality-release` 质控放行

### 输入

- `injections`：按**实际上机注入顺序**排列的针次，**最多 48 针**，至少 1 针；每针带全序列
  唯一的 `injection_id` 与有限读数 `reading`。针次用 `type` 区分：
  - `qc` 质控针：只含注入号与读数；
  - `sample` 样本针：额外带 `sample_id`（样本号）与可选 `retest_of`。
- `acceptance_range`：质控读数的**合格闭区间** `[lower, upper]`（含边界），质控读数落入
  区间判为在控，否则失控。
- 复测规则：复测针通过 `retest_of` 引用**同样本、更早注入**的一支样本针；首针留空。
  引用不存在、指向质控针、指向后续针或跨样本引用一律返回 422。

### 质控窗口规则（逐针）

每支样本针只看其**前后最近的两支质控针**（中间隔着其他针不影响"最近"）：

| 窗口状态 | 条件 | `window_reason` |
| --- | --- | --- |
| `released` | 两侧质控齐全且都在控 | `null` |
| `pending` | 缺前侧 / 缺后侧 / 两侧都缺质控，待补 | `missing_prev_qc` / `missing_next_qc` / `missing_both_qc` |
| `quarantined` | 两侧齐全，但至少一侧失控 | `qc_out_of_control` |

### 复测链与样本级结论

同一样本的引用关系构成一片有向森林（边：后续针 → 被引用的更早针）。

- **唯一复测链**（一个起始根、一个末针，无分叉）：只放行**末针**——
  - 末针窗口 `released` → 样本 `released`，返回末针注入号与读数；
  - 末针窗口 `pending` → 样本 `pending`（`tail_pending`），待补质控后重判；
  - 末针窗口 `quarantined` → 样本 `quarantined`（`tail_quarantined`），整链隔离；
  - **末针不能入库时绝不退回链上任何更早的读数。**
- **链冲突**（样本 `conflict`，原因码 `chain_conflict`），全部针次扣留：
  - `retest_fork`：同一复测树中一支针被多支后续针引用（复测分叉）；
  - `multiple_unrelated_chains`：存在多个互不引用的起始针（多条无关联链）；
  - `both`：两种情形同时存在。

### 响应

- `summary`：总针数、质控在控/失控计数、各处置状态的样本数；
- `samples`：按样本首次出现顺序排列，每个样本含状态、扣留原因（机器可读 `reason_code`
  + 中文 `reason_message`）、复测链末针列表、可入库读数，以及逐针窗口判定
  （含前后质控注入号、1 基位序、是否末针）。

### 快速验证（Docker Compose）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000/docs ，/quality-release 的 Examples 下拉框内置：
#   normal_release  常规放行 + 序列末端待补
#   qc_failure      质控失控 -> 末针隔离、不退回旧读数
#   chain_conflict  复测分叉 + 多条无关联链 -> 冲突扣留
```

curl 示例：

```bash
curl -X POST http://localhost:8000/quality-release \
  -H 'Content-Type: application/json' \
  -d '{
    "acceptance_range": {"lower": 9.0, "upper": 11.0},
    "injections": [
      {"type": "qc", "injection_id": "QC-01", "reading": 10.0},
      {"type": "sample", "injection_id": "I-01", "sample_id": "SMP-A", "reading": 5.1},
      {"type": "sample", "injection_id": "I-02", "sample_id": "SMP-A",
       "reading": 5.3, "retest_of": "I-01"},
      {"type": "qc", "injection_id": "QC-02", "reading": 10.4}
    ]
  }'
```

### 校验规则（不满足返回 422）

- 针次 1–48 支；`injection_id` 全序列唯一、非空；`sample_id` 非空；
- 所有读数与区间端点必须是有限实数（拒绝 NaN/Infinity）；
- 合格区间下限不得大于上限；
- `retest_of` 必须指向存在的、同样本的、更早注入的样本针；
- 不接受未定义字段，`type` 只允许 `qc` / `sample`。

---

## 二、`POST /match` 采样-检测配对

### 配对规则

- 输入两组记录：采样登记、检测记录，**每组最多 12 条**。
- 每条记录：组内唯一 `id`、可重复的容器条码 `barcode`、UTC 时间 `timestamp`
  （ISO 8601，必须带时区，推荐 `Z` 后缀）。
- 两条记录**条码相同且 UTC 时间差 ≤ 10 分钟（含边界）**才可配对。
- 每条记录至多参与一对。
- 优化目标（字典序）：**先使配对数最多，再使时间差总和最小**。

### 输出语义

| 状态 | 含义 | 处置建议 |
| --- | --- | --- |
| `certain` | 确定配对：在所有同等最优方案中都成立 | 可进入分析库 |
| `review` | 待核查：存在同等最优方案分歧（对象不定或时有时无） | 交质控员人工核查 |
| `unmatched` | 始终未配对：任何同等最优方案都不包含它 | 不进入分析库 |

`review` 记录附带：

- `matched_in_all_optimal`：`true` 表示"始终被配对但对象不定"，`false` 表示
  "部分最优方案中未配对"；
- `possible_partners`：在至少一个同等最优方案中可行的配对对象及时间差。

### 请求示例

```bash
curl -X POST http://localhost:8000/match \
  -H 'Content-Type: application/json' \
  -d '{
    "sampling_records": [
      {"id": "S-01", "barcode": "BC-100", "timestamp": "2026-11-14T08:00:00Z"}
    ],
    "detection_records": [
      {"id": "D-01", "barcode": "BC-100", "timestamp": "2026-11-14T08:04:00Z"}
    ]
  }'
```

### 校验规则（不满足返回 422）

- 每组 0–12 条记录；
- 同组内 `id` 不得重复；
- `id`、`barcode` 非空；
- `timestamp` 必须带时区（带偏移量的时间会归一化为 UTC 再比较）；
- 不接受未定义的字段。

### 算法说明

建模为二分图最小费用最大流：左部采样记录、右部检测记录，候选边权为时间差
（毫秒整数）。持续沿最短路增广直到不可增广，即得"配对数最大、同流量下总时间差
最小"的字典序最优匹配。

确定性判定通过精确重算完成，而非启发式：

- **确定配对**：禁用该候选边后最优值（配对数, 总时间差）变差 ⟺ 它存在于所有
  同等最优方案；
- **可行对象**：强制选入该候选边（删除两端点）后仍能达到整体最优值 ⟺ 它存在于
  某个同等最优方案；
- **始终被配对**：删除该记录后最优值变差 ⟺ 它在所有同等最优方案中都被配对。

每组 ≤ 12 条记录时候选边 ≤ 144 条，重算约 200 次微型流，单次请求开销在毫秒级。

---

## 部署与本地开发

服务监听 `http://localhost:8000`：

- `POST /match`、`POST /quality-release` —— 两个业务接口；
- `GET /docs` —— Swagger UI，两个接口均内置 Examples 演示请求；
- `GET /health` —— 健康检查（Compose healthcheck 使用）。

```bash
# Docker Compose 一键启动
docker compose up --build

# 本地开发与测试
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest                 # 运行全部测试
uvicorn app.main:app --reload
```

## 目录结构

```
app/
  main.py             # FastAPI 入口、响应组装与 422 异常净化
  matching.py         # /match 配对核心算法（最小费用最大流 + 确定性判定）
  schemas.py          # /match 请求/响应模型与校验
  examples.py         # /match 的三组 /docs 演示请求
  quality.py          # /quality-release 核心算法（质控窗口 + 复测链完整性）
  quality_schemas.py  # /quality-release 请求/响应模型与校验
  quality_examples.py # /quality-release 的三组 /docs 演示请求
tests/                # 算法单元测试与 API 集成测试（共 60 项）
Dockerfile
docker-compose.yml
```
