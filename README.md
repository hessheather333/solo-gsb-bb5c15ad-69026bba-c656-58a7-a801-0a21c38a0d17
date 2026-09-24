# 传感器遥测汇聚 API

基于 **FastAPI + SQLite** 的传感器遥测汇聚服务，支持事件时间水位线、允许迟到窗口、幂等去重与迟到数据修正，提供**设备可版本化标签**（全量替换、键名规范化、乐观并发 `if_version`、按当前标签筛选设备），并提供**设备组同步窗口汇总**（固定成员组，sum/avg，全成员封存才出数，迟到修正原位升版）、**组窗口修订审计**（首次 ready 与每次升版在同事务追加不可变快照，提供修订列表与两版本 diff）、**封存窗口稳健异常检测**（对已封存窗口事件按中位数/MAD 打稳健分数，纯只读）、**跨设备窗口检索**（按时间范围、状态与设备标签精确筛选，绑定筛选条件的稳定游标分页）、**设备级高值告警**（触发/恢复双阈值滞回状态机，事件写入驱动状态转换，规则与状态持久化，批次重试不重复产生状态变化）、**设备告警周期查询**（按转换流水自增顺序配对 entered/cleared 成周期，以流水处理时刻为起止，裁剪到指定 UTC 区间给出各周期秒数与总告警秒数，未解除周期计至 end 与查询时刻较早者，纯只读）、**设备缺报区间查询**（按窗口空隙计算连续缺报段，水位线未越过封存条件的窗口不报缺，迟到填空后自动缩短/拆分，纯只读）与**设备每日同窗基线偏移查询**（按 UTC 每天同一窗位回看已封存窗口，以窗口均值的中位数/MAD 计算目标窗口偏移分数，缺失或未封存样本不以临近窗口代替，样本不足明确返回，迟到修正后再次查询反映最新聚合，纯只读）。

## 核心语义

- **水位线（watermark）**：每个设备维护一条持久化的事件时间水位线，等于该设备已见最大 `event_time`，单调递增、落盘保存，重启不丢失。
- **时间窗**：按 `window_size_seconds`（默认 60s）对齐 epoch 的滚动窗口，按设备独立维护。
- **窗口封存**：当 `watermark >= window_end + allowed_lateness_seconds`（默认 30s）时，窗口状态从 `open` 变为 `sealed`。
- **幂等去重**：同一 `(device_id, event_id)` 重复提交只生效一次，返回 `duplicate`，不影响聚合结果。
- **迟到修正**：窗口封存后到达的事件标记为 `late`，仍计入窗口聚合，同时生成一条修正记录（correction），窗口的 `correction_count` 递增。
- 批次内事件按提交顺序处理，与流式处理语义一致。

### 设备可版本化标签

- **全量替换**：`PUT /devices/{id}/labels` 以"整批替换"语义写入标签（最多 20 个 key/value，超出返回 `422`）；旧标签在同一 SQLite 写事务内删除、新标签写入，**每次成功写入都令 `version` 加一**（从 1 起单调递增，即使内容与上次完全相同也升版）。写入空列表即清空标签，版本同样递增。
- **键名规范化**：写入时键名先去首尾空白再转小写，且必须匹配 `^[a-z0-9][a-z0-9_.-]*$`（最长 63）；值只去首尾空白、不转小写，且不能为空（最长 256）。规范化后出现重复键（如 `"Room"` 与 `" room "`）返回 `400`，设备不存在返回 `404`；所有校验失败均**不改动任何数据**。
- **当前版本读取**：`GET /devices/{id}/labels` 返回当前标签键值对、`version` 与 `updated_at`；从未写入标签的设备返回 `version=0`、空标签。
- **乐观并发控制**：PUT 请求可带 `if_version`，服务在写锁事务内比对其与当前版本，不一致返回 `409`（响应中带回当前版本）且**不写入任何数据**；不带该字段时不做并发校验。
- **按当前标签筛选**：`GET /devices?label_key=k&label_value=v` 返回当前标签精确匹配该键值对的设备；只给 `label_key` 返回带有该键的设备；只给 `label_value` 不给 `label_key` 返回 `422`。筛选只反映**最新版本**的标签，旧版本已被替换的键值不会命中。查询键名与写入时同样规范化。

### 设备组同步窗口汇总

- **固定成员组**：创建组时显式给出成员设备、`aggregation`（`sum` 或 `avg`）以及窗口配置；所有成员设备必须已存在，且 `window_size_seconds` / `allowed_lateness_seconds` 与组配置完全一致（否则分别返回 404 / 409），保证各成员窗口在同一时间轴对齐、封存口径一致。成员在组创建后不可变更。
- **同步封存才出数**：对每个 `window_start`，只有**全部成员**的设备窗口都已封存（sealed）时才生成组汇总（`status=ready`）；任一成员窗口仍 open 或尚无数据时，组窗口保持 `pending`、`value=null`，**绝不用部分成员提前计算**。
- **聚合口径**：`sum` 为各成员窗口 `sum` 之和；`avg` 为各成员窗口 `avg`（窗口内事件均值）的算术平均，每个固定成员权重相同。
- **修正原位升版**：成员窗口因迟到事件被修正后，已 ready 的组汇总在原记录上重算，`revision` 加一。系统以各成员窗口 `correction_count` 之和作为修正签名：同一 `event_id` 重试会被幂等去重、不产生新修正，签名不变，因此 **revision 不会重复升版**。ready 之前发生的修正包含在首次结果中，`revision` 从 1 起。
- 组也可以在成员窗口已封存之后创建，创建时即补算历史窗口。

### 组窗口修订审计（不可变快照）

- **同事务留档**：组窗口**首次 ready**、以及迟到修正令 **revision 升版**时，在与组汇总写入的**同一事务**内追加一份不可变快照，记录当时的 group `value`，以及每个固定成员窗口当时的 `count`、`sum`、`avg`、`correction_count`。
- **不重复留档**：快照以 `(group_id, window_start, revision)` 唯一约束兜底，重复上报（幂等去重、签名未推进）绝不会产生第二份同版本快照。
- **不可变、不可覆盖**：审计表只允许 `INSERT`，数据库触发器会直接中止任何 `UPDATE`/`DELETE`；后续修正只追加新 revision 的快照，**历史快照永不被覆盖**。
- **初始化只补基线**：旧版本数据库升级启动时，只对**已 ready** 但尚无快照的组窗口补一份**当前 revision** 的 baseline（标记 `kind=baseline`），**绝不伪造** ready 之后可能存在过的早期版本；新库及已有快照的窗口不受影响。
- **审计接口**：提供修订列表与两版本 diff：
  - 修订列表按 revision 升序返回每个快照的 `value`、`kind`（`revision`/`baseline`）与成员数；
  - diff 按**成员顺序**返回每个**发生变化**成员的前后贡献（sum 聚合贡献为窗口 `sum`，avg 聚合贡献为窗口 `avg`）与 `count/sum/avg/correction_count` 的数值差，未变化成员不列出；
  - 版本参数非法（缺失、`<1`、`from >= to`）返回 `422`；组、窗口或任一修订不存在返回 `404`。

### 封存窗口稳健异常检测（只读）

- **只检测已封存窗口**：`GET /devices/{id}/anomalies?window_start=...&threshold=...` 对指定窗口内的全部事件（**包含封存后到达的迟到事件**）按**中位数**与 **MAD（median absolute deviation，绝对偏差中位数）** 计算稳健分数，不受极端离群值拉偏：
  - `score = 0.6745 * |value - median| / MAD`（即修正 z 分数，0.6745 为正态分布下 `MAD ≈ 0.6745σ` 的换算系数）；
  - 只返回 `score > threshold` 的事件，`threshold` 默认 **3.5**，必须 **大于 0**（传 `0` 或负数返回 `422`），可按需调大（少报）或调小（多报）。
- **MAD 为 0 的退化情形**：超过半数事件等于中位数时 MAD=0，除以 0 无定义。此时按需求把所有**不等于中位数**的事件一律判为异常返回，对应 `score` 字段为 `null`（JSON 中可判空）；若窗口内全部事件相等则没有异常。
- **状态码**：设备不存在或该设备在 `window_start` 处没有窗口（从未有事件落入）返回 `404`；窗口存在但仍是 `open`（未封存）返回 `409`。
- **纯只读**：检测过程只执行查询，不修改事件、窗口聚合（count/sum/avg/min/max）、封存状态与修正计数。

### 跨设备窗口检索（稳定游标分页，只读）

- **精确筛选**：`GET /windows/search` 跨设备检索窗口，可选参数：
  - `start` / `end`：窗口时间范围，对 `window_start` 取**半开区间 `[start, end)`**（ISO 8601，均可单独给出）；`end <= start` 返回 `422`；
  - `status`：`open` 或 `sealed`，按窗口状态精确筛选；
  - `label_key` / `label_value`：按设备**当前**标签筛选，语义与 `GET /devices` 完全一致（键值对精确匹配 / 只按键匹配 / 只给值返回 `422`，键名与值同样做规范化）；
  - `limit`：每页条数，默认 `100`，范围 `1..1000`（越界 `422`）。
- **排序与返回**：结果按 `window_start`、`device_id` 升序，每项是窗口聚合与设备标识（字段结构与 `GET /devices/{id}/windows` 完全一致），响应形如 `{"windows": [...], "next_cursor": "..."}`；没有更多数据时 `next_cursor` 为 `null`。
- **稳定游标分页**：`next_cursor` 是**不透明**字符串，内部用 HMAC-SHA256 签名，**绑定本次的全部筛选条件**与首次查询时的行快照上界，默认 **1 小时**过期。翻页时把上一页的 `next_cursor` 原样作为 `cursor` 传回即可（筛选参数必须原样带上，`limit` 可在翻页间调整）。
  - **新增数据不影响进行中的遍历**：游标锚定首次查询时的稳定集合，翻页期间新写入的窗口（无论时间早晚）都不会进入本次遍历，因此**不会在后续页重复出现，也不会插入或挤出造成遗漏**；需要查看新增数据时不带 `cursor` 重新发起检索即可。
  - 游标被篡改或伪造、载荷损坏、**已过期**、或与当前请求的筛选条件不一致，一律返回 **`400`**，客户端应重新发起无游标检索。
- **纯只读**：检索不修改任何数据，也不影响现有接口。

### 设备级高值告警（双阈值滞回状态机）

- **设置规则**：`PUT /devices/{id}/alert-rule` 为设备设置两条线——较高的**触发阈值** `trigger_threshold` 与较低的**恢复阈值** `recovery_threshold`（重复 PUT 为整批替换）。阈值必须是有限数，且**恢复阈值严格低于触发阈值**（相等也拒绝，否则没有滞回区间）；任一不合法返回 `400`，且请求在开启写事务**之前**被拒绝，**旧规则原样保留、绝不被覆盖**。设备不存在返回 `404`。
- **查询状态**：`GET /devices/{id}/alert` 返回当前生效规则与状态：`alerting`（是否告警中）、累计进入次数 `alarm_count`、累计解除次数 `recovery_count`、最近驱动事件（`last_value`/`last_event_id`/`last_event_time`）、最近转换类型 `last_transition`（`entered`/`cleared`）及最近进入/解除时间。设备不存在返回 `404`；**设备存在但从未配置规则返回 `404`**（无规则即没有状态机）；配置规则后尚无事件时计数均为 0、`last_transition` 为 `null`。
- **事件写入驱动状态转换（滞回防抖）**：事件经 `POST /ingest` 写入时（**迟到事件同样参与**），在与窗口聚合相同的事务内按提交顺序逐事件驱动：
  - 当前**未告警**且 `value >= trigger_threshold` → **首次越线进入告警**（`entered`，`alarm_count +1`）；告警中再来高值**不重复告警**；
  - 当前**告警中**且 `value <= recovery_threshold` → **降至恢复线解除告警**（`cleared`，`recovery_count +1`）；
  - `recovery_threshold < value < trigger_threshold` 的**中间区间保持原状态**——值在阈值附近上下波动不会反复告警/解除。
  - 边界取等：恰好等于触发线即进入，恰好等于恢复线即解除。
- **规则替换不改变状态**：PUT 新阈值只影响此后的事件评估，不会基于历史数据追溯、也不会令当前 `alerting` 翻转。
- **持久化与防重**：规则、当前状态与每次状态转换流水都落盘在 SQLite 中（状态与聚合在**同一 ingest 写事务**内更新，ingest 使用 `BEGIN IMMEDIATE` 串行化并发批次），服务重启后状态原样恢复。状态转换只追加流水，且 `(device_id, event_id)` 上有唯一约束兜底；**批次重试中的重复事件在 ingest 幂等去重阶段即被跳过，绝不会第二次驱动状态机**（重启后重试亦然）。

### 设备告警周期查询（只读）

- **接口**：`GET /devices/{id}/alert-episodes?start=...&end=...` 供值班人员查看指定 UTC 时间段内的告警周期（episode）与总告警秒数。`start`/`end` 均必填（ISO 8601，不带时区按 UTC 处理），取**半开区间 `[start, end)`**；`end <= start` 或参数缺失/非法返回 `422`，设备不存在或**未配置告警规则**返回 `404`。
- **周期配对**：按告警转换流水（`device_alert_transitions`）的**自增顺序**把 `entered` 与下一条 `cleared` 配对成一个周期，周期的进入/解除时刻取流水记录的**处理时刻 `created_at`**；迟到事件只按处理顺序追加流水，**不按 `event_time` 重排**。
- **区间裁剪**：只返回与 `[start, end)` **相交**的周期（端点相接或零长周期不算相交），逐条给出 `entered_at`、`cleared_at` 与**裁剪后的区间内秒数** `seconds`；尚未解除的周期 `cleared_at=null`，只计至 `end` 与**查询时刻**的较早者。
- **总秒数**：`total_seconds` 为各明细 `seconds` 之和；无相交周期返回空明细 `[]` 与 `0`。接口纯只读，不改变告警状态与转换流水。

### 设备缺报区间查询（只读）

- **接口**：`GET /devices/{id}/missing-windows?start=...&end=...` 查询半开区间 `[start, end)` 内的连续缺报段，返回每段的起止时间（`start` 为首个缺失窗口起点、`end` 为末个缺失窗口终点，半开）与缺失窗口数 `missing_windows`，按段起点升序。`start`/`end` 均必填且必须**对齐设备窗口大小**（如 60s 网格上的整分钟）；`end <= start` 或边界未对齐返回 `422`，设备不存在返回 `404`，设备尚无数据返回空列表。
- **缺报判定**：一个对齐窗口位置同时满足以下三条才计为缺失——
  - 不早于设备**最早有数据窗口的下一个窗口**（设备尚未开始上报之前的历史不追缺）；
  - 水位线已越过该窗口的封存条件（`watermark >= window_end + allowed_lateness_seconds`），**尚未到达封存条件的窗口一律不报缺**；
  - `windows` 表中没有该位置的窗口记录（已有记录的位置无论 open/sealed 都不算缺）。
- **按空隙计算**：实现只读取查询范围内的现存窗口记录，用相邻窗口之间的空隙宽度除以窗口大小得到缺失窗口数，**不逐窗遍历整个时间跨度**，大范围查询代价与范围内现存窗口数成正比。
- **迟到填空自动生效**：迟到事件首次落入空窗会新建（并封存）该窗口记录，后续查询立即按最新空隙**缩短或拆分**缺报段；接口纯只读，不追改任何已有聚合。

### 设备每日同窗基线偏移查询（只读）

- **接口**：`GET /devices/{id}/daily-baseline?window_start=...&days=...&threshold=...` 以目标窗口为基准，按 **UTC 每天相同窗位**（`window_start - k*86400`，`k=1..days`）回看历史窗口，判断目标窗口均值相对历史同窗基线的偏移是否越界。
  - `window_start`：目标窗口起始时间（ISO 8601，须落在设备窗口网格上）；
  - `days`：回看天数，范围 **3..30**（越界 `422`）；
  - `threshold`：正数越界阈值（`> 0`，否则 `422`），偏移分数**严格大于**该值才判越界。
- **前置条件**：
  - 设备窗口秒数必须**可整除一天**（`86400 % window_size_seconds == 0`，如 60s、3600s、86400s；61s 等返回 `422`），否则"每天同一窗位"无法逐日对齐；
  - 目标窗口必须**已封存**：设备不存在返回 `404`，目标窗口不存在返回 `404`，窗口仍 `open` 返回 `409`。
- **样本采集（不以临近窗口代替）**：仅当回看窗位的窗口**存在且已封存**时计为有效样本（包含封存后的迟到修正，取最新 `sum/count` 与均值）；该窗位缺失（无窗口记录）或仍 open 一律**跳过**，**绝不会用相邻窗口或相邻天数代替**。
- **基线与打分**：基线取有效窗口均值的**中位数**与 **MAD（绝对偏差中位数，不乘 0.6745 归一化系数）**，偏移分数 `score = |目标均值 - 中位数| / MAD`，`score > threshold` 判越界（`breached=true`）。
- **样本不足**：有效样本**不足 3 个**时返回 `status="insufficient_samples"`，并给出实际 `sample_count` 与已采集到的样本明细；此时不计算基线/分数（`baseline_median`、`baseline_mad`、`deviation`、`score` 均为 `null`），`breached` 也为 `null`（无法判定，而非不越界）。样本充足时 `status="ok"`。
- **MAD 为 0 的退化情形**：历史同窗均值全部相同时 MAD=0——
  - 目标均值与中位数**相同**（同值偏移）：`score=0`、`breached=false`；
  - 目标均值与中位数**不同**（异值偏移）：除以 0 无定义，`score=null` 且 `breached=true`（无论 `threshold` 多大）。
- **迟到修正即时反映**：迟到事件原位更新目标窗口或历史样本窗口的聚合（同 `POST /ingest` 既有语义），**再次查询立即得到最新的目标均值、基线与越界结论**；幂等重试不产生新修正、结论不变。
- **返回内容**：目标窗口信息、回看天数、`status`、有效样本数与每个样本（`window_start`/`window_end`/`offset_days`/`avg`，按 `window_start` 升序）、`target_avg`、`baseline_median`、`baseline_mad`、`deviation`、`score`、`threshold` 与 `breached`。接口纯只读。

## 启动

### Docker（推荐，一键启动）

确保已安装 Docker Engine 与 Docker Compose 插件（`docker compose version` 能正常输出版本号；Linux 上通常随 Docker Engine 一起安装，亦可通过系统包管理器安装 `docker-compose-plugin`）。然后在**项目根目录**（`docker-compose.yml` 所在目录）执行：

```bash
# 构建镜像并以前台方式启动（加 -d 可后台运行）
docker compose up --build

# 后台启动 / 查看日志 / 停止
docker compose up -d --build
docker compose logs -f api
docker compose down
```

启动后可立即验证服务是否就绪：

```bash
curl http://localhost:8000/health      # => {"status":"ok"}
curl http://localhost:8000/windows/search   # 跨设备窗口检索（空库时返回 {"windows":[],"next_cursor":null}）
```

服务监听并映射宿主机 **8000** 端口：

- 健康检查：http://localhost:8000/health
- Swagger 交互式文档：http://localhost:8000/docs
- ReDoc 文档：http://localhost:8000/redoc

停止服务用 `Ctrl+C`（后台模式则 `docker compose down`）。数据保存在 Docker 卷 `telemetry-data` 中，`docker compose down` 不会删除；如需清空可执行 `docker compose down -v`。

Compose 还配置了基于 `/health` 的健康检查（仅用镜像自带 Python，无需额外工具）：可用 `docker compose ps` 查看 `healthy`/`unhealthy` 状态。若端口 8000 已被占用，可在 `docker-compose.yml` 中把 `"8000:8000"` 左侧改为其它宿主端口（如 `"18000:8000"`），随后通过新端口访问。

或纯 Docker：

```bash
docker build -t telemetry-api .
docker run -p 8000:8000 -v telemetry-data:/data telemetry-api
```

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

启动后同样通过 **http://localhost:8000** 访问（文档见 `/docs`）。

### 必要配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TELEMETRY_DB_PATH` | `telemetry.db`（本地）/ `/data/telemetry.db`（容器） | SQLite 数据库文件路径；容器内 `/data` 已挂载卷持久化 |

设备默认窗口参数（也可经 `PUT /devices/{id}/config` 逐设备覆盖）：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `window_size_seconds` | `60` | 滚动窗口大小（秒） |
| `allowed_lateness_seconds` | `30` | 允许迟到时间（秒），超过 `window_end + 该值` 窗口封存 |

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

## API 示例

### 1. 配置设备（可选，不配置则首次上报时按默认值自动注册）

```bash
curl -X PUT http://localhost:8000/devices/sensor-001/config \
  -H 'Content-Type: application/json' \
  -d '{"window_size_seconds": 60, "allowed_lateness_seconds": 30}'
```

> 设备已有数据后不允许修改 `window_size_seconds`（返回 409），`allowed_lateness_seconds` 可随时调整。

### 2. 提交事件批次

```bash
curl -X POST http://localhost:8000/ingest \
  -H 'Content-Type: application/json' \
  -d '{
    "device_id": "sensor-001",
    "events": [
      {"event_id": "evt-1", "event_time": "2026-09-22T10:00:10Z", "value": 21.5},
      {"event_id": "evt-2", "event_time": "2026-09-22T10:00:40Z", "value": 22.0},
      {"event_id": "evt-3", "event_time": "2026-09-22T10:02:00Z", "value": 23.1}
    ]
  }'
```

响应（水位线推进到 10:02:00，窗口 `[10:00, 10:01)` 因 `10:01:00 + 30s <= 10:02:00` 被封存）：

```json
{
  "device_id": "sensor-001",
  "watermark": "2026-09-22T10:02:00Z",
  "accepted": 3, "duplicates": 0, "late": 0,
  "results": [
    {"event_id": "evt-1", "status": "accepted", "window_start": "2026-09-22T10:00:00Z"},
    {"event_id": "evt-2", "status": "accepted", "window_start": "2026-09-22T10:00:00Z"},
    {"event_id": "evt-3", "status": "accepted", "window_start": "2026-09-22T10:02:00Z"}
  ]
}
```

### 3. 重复提交 + 迟到数据

```bash
curl -X POST http://localhost:8000/ingest \
  -H 'Content-Type: application/json' \
  -d '{
    "device_id": "sensor-001",
    "events": [
      {"event_id": "evt-1", "event_time": "2026-09-22T10:00:10Z", "value": 21.5},
      {"event_id": "evt-4", "event_time": "2026-09-22T10:00:25Z", "value": 20.9}
    ]
  }'
```

`evt-1` 为重复事件被去重；`evt-4` 落入已封存窗口，标记为 `late` 并生成修正记录：

```json
{
  "accepted": 0, "duplicates": 1, "late": 1,
  "results": [
    {"event_id": "evt-1", "status": "duplicate", "window_start": null},
    {"event_id": "evt-4", "status": "late", "window_start": "2026-09-22T10:00:00Z"}
  ]
}
```

### 4. 查询窗口聚合

```bash
curl http://localhost:8000/devices/sensor-001/windows
# 可按状态过滤：curl 'http://localhost:8000/devices/sensor-001/windows?status=sealed'
```

```json
[
  {
    "device_id": "sensor-001",
    "window_start": "2026-09-22T10:00:00Z",
    "window_end": "2026-09-22T10:01:00Z",
    "status": "sealed",
    "count": 3, "sum": 64.4, "avg": 21.4667, "min": 20.9, "max": 22.0,
    "correction_count": 1,
    "late_event_count": 1,
    "sealed_at": "2026-09-22T10:02:31Z"
  },
  {
    "device_id": "sensor-001",
    "window_start": "2026-09-22T10:02:00Z",
    "window_end": "2026-09-22T10:03:00Z",
    "status": "open",
    "count": 1, "sum": 23.1, "avg": 23.1, "min": 23.1, "max": 23.1,
    "correction_count": 0,
    "late_event_count": 0,
    "sealed_at": null
  }
]
```

### 5. 查询修正记录

```bash
curl http://localhost:8000/devices/sensor-001/corrections
```

```json
[
  {
    "id": 1,
    "device_id": "sensor-001",
    "event_id": "evt-4",
    "window_start": "2026-09-22T10:00:00Z",
    "event_time": "2026-09-22T10:00:25Z",
    "value": 20.9,
    "created_at": "2026-09-22T10:02:45Z"
  }
]
```

### 6. 查询设备水位线

```bash
curl http://localhost:8000/devices/sensor-001
```

```json
{
  "device_id": "sensor-001",
  "window_size_seconds": 60,
  "allowed_lateness_seconds": 30,
  "watermark": "2026-09-22T10:02:00Z",
  "event_count": 4,
  "open_windows": 1,
  "sealed_windows": 1,
  "created_at": "2026-09-22T10:00:01Z"
}
```

### 7. 写入设备标签（版本化、全量替换）

```bash
curl -X PUT http://localhost:8000/devices/sensor-001/labels \
  -H 'Content-Type: application/json' \
  -d '{
    "labels": [
      {"key": "Room", "value": "  Hall-A "},
      {"key": "model.id", "value": "x1"}
    ]
  }'
```

键名会在事务内规范化（去首尾空白、转小写），值只去首尾空白：

```json
{
  "device_id": "sensor-001",
  "version": 1,
  "labels": {"model.id": "x1", "room": "Hall-A"},
  "updated_at": "2026-09-22T10:05:00Z"
}
```

- 每次成功 PUT 都整批替换并令 `version` 递增；写 `{"labels": []}` 即清空标签。
- 最多 20 个键值对（超出 `422`）；非法键名或规范化后重复键返回 `400`，设备不存在返回 `404`，均不改数据。
- 带 `if_version` 做乐观并发校验：版本不匹配返回 `409` 且不写入任何数据，客户端重新 GET 最新版本后重试即可：

```bash
curl -X PUT http://localhost:8000/devices/sensor-001/labels \
  -H 'Content-Type: application/json' \
  -d '{
    "if_version": 1,
    "labels": [{"key": "room", "value": "Hall-B"}]
  }'
# 若设备当前已是 version 2 => 409 {"detail": "label version conflict: if_version=1 does not match current version 2"}
```

### 8. 读取当前标签与版本

```bash
curl http://localhost:8000/devices/sensor-001/labels
```

```json
{
  "device_id": "sensor-001",
  "version": 1,
  "labels": {"model.id": "x1", "room": "Hall-A"},
  "updated_at": "2026-09-22T10:05:00Z"
}
```

从未写入过标签的设备返回 `version: 0`、空标签。

### 9. 按当前标签筛选设备

```bash
# 精确匹配某个键值对
curl 'http://localhost:8000/devices?label_key=room&label_value=Hall-A'

# 只按键：所有带 room 标签的设备
curl 'http://localhost:8000/devices?label_key=room'

# 不带筛选参数：全部设备
curl http://localhost:8000/devices
```

只给 `label_value` 不给 `label_key` 返回 `422`。响应为设备对象列表（结构同 `GET /devices/{id}`），按 `device_id` 升序。

### 10. 创建设备组（固定成员，sum/avg）

成员设备必须**先存在**且窗口配置与组一致（可先用第 1 步的 config 接口或上报事件完成注册）：

```bash
curl -X POST http://localhost:8000/groups \
  -H 'Content-Type: application/json' \
  -d '{
    "group_id": "floor-1",
    "member_ids": ["sensor-001", "sensor-002"],
    "aggregation": "avg",
    "window_size_seconds": 60,
    "allowed_lateness_seconds": 30
  }'
```

- 成员不存在 → `404`；窗口配置不一致或组 ID 已存在 → `409`；成员列表重复 → `400`。
- 组创建后即对成员已有窗口做一次补算。

### 11. 按窗口查询组汇总

```bash
# 全部窗口（pending + ready）
curl http://localhost:8000/groups/floor-1/windows

# 可按状态 / window_start 过滤
curl 'http://localhost:8000/groups/floor-1/windows?status=pending'
curl 'http://localhost:8000/groups/floor-1/windows?window_start=2026-09-22T10:00:00Z'

# 查询单个窗口
curl http://localhost:8000/groups/floor-1/windows/2026-09-22T10:00:00Z
```

仅当 `sensor-001`、`sensor-002` 在 `10:00` 的窗口都封存后才 ready；此前为 pending 且 `value` 为 `null`：

```json
[
  {
    "group_id": "floor-1",
    "window_start": "2026-09-22T10:00:00Z",
    "window_end": "2026-09-22T10:01:00Z",
    "status": "pending",
    "value": null,
    "member_count": 2,
    "sealed_member_count": 1,
    "revision": 0,
    "ready_at": null,
    "created_at": "2026-09-22T10:02:31Z",
    "updated_at": "2026-09-22T10:02:31Z"
  }
]
```

全部封存后：`status` 变为 `ready`，`revision` 从 1 起。此后任一成员窗口收到迟到事件，该记录原位重算、`revision` 递增；同一 `event_id` 重试不升版。

### 12. 查询 pending 成员明细

```bash
curl http://localhost:8000/groups/floor-1/windows/2026-09-22T10:00:00Z/pending
```

`reason=open` 表示成员窗口尚未封存；`reason=missing` 表示该成员在该窗口还没有任何事件（窗口不存在）。ready 窗口返回 `[]`，尚无任何成员数据的窗口返回 `404`：

```json
[
  {
    "device_id": "sensor-002",
    "window_start": null,
    "window_end": "2026-09-22T10:01:00Z",
    "window_status": null,
    "reason": "missing"
  }
]
```

### 13. 查询组窗口修订列表（不可变快照）

组窗口每次首次 ready、以及迟到修正升版时，都会在同一事务留一份不可变快照：

```bash
curl http://localhost:8000/groups/floor-1/windows/2026-09-22T10:00:00Z/revisions
```

```json
[
  {"revision": 1, "value": 62.3, "kind": "revision", "created_at": "2026-09-22T10:02:31Z", "member_count": 2},
  {"revision": 2, "value": 67.8, "kind": "revision", "created_at": "2026-09-22T10:03:10Z", "member_count": 2}
]
```

- 尚未 ready（pending）的窗口返回 `[]`；组或窗口不存在返回 `404`。
- 旧版本数据库升级后首次启动，只会为已 ready 窗口补一份**当前 revision** 的 `baseline`，不伪造早期版本。
- 快照只追加、不可修改：任何对审计表的 `UPDATE`/`DELETE` 都会被数据库触发器中止；同一 revision 重复上报也受唯一约束保护，不会重复留档。

### 14. 对比两个 revision（diff）

```bash
curl 'http://localhost:8000/groups/floor-1/windows/2026-09-22T10:00:00Z/revisions/diff?from_revision=1&to_revision=2'
```

响应按成员顺序只列出发生变化的成员，给出前后贡献与各项数值差（`sum` 聚合下贡献为成员窗口 `sum`，`avg` 聚合下为成员窗口 `avg`）：

```json
{
  "group_id": "floor-1",
  "window_start": "2026-09-22T10:00:00Z",
  "aggregation": "sum",
  "from_revision": 1,
  "to_revision": 2,
  "from_value": 62.3,
  "to_value": 67.8,
  "value_delta": 5.5,
  "members": [
    {
      "device_id": "sensor-001",
      "position": 0,
      "before": {"device_id": "sensor-001", "position": 0, "count": 3, "sum": 41.4, "avg": 13.8, "correction_count": 0, "contribution": 41.4},
      "after":  {"device_id": "sensor-001", "position": 0, "count": 4, "sum": 46.9, "avg": 11.725, "correction_count": 1, "contribution": 46.9},
      "count_delta": 1,
      "sum_delta": 5.5,
      "avg_delta": -2.075,
      "correction_count_delta": 1,
      "contribution_delta": 5.5
    }
  ]
}
```

- `from_revision` / `to_revision` 均必填且 `>= 1`，且 `from_revision < to_revision`，否则返回 `422`。
- 组、窗口不存在，或指定的任一 revision 无快照（例如 baseline 之前被伪造的旧版本号）返回 `404`。

### 15. 封存窗口稳健异常检测（中位数 / MAD）

窗口必须**已封存**；检测范围包含迟到事件，接口纯只读。下例窗口 `[10:00, 10:01)` 已封存，其中大部分值贴近 21.5，`evt-spike` 为离群点：

```bash
curl 'http://localhost:8000/devices/sensor-001/anomalies?window_start=2026-09-22T10:00:00Z'
# 自定义阈值（默认 3.5，必须 > 0）：
curl 'http://localhost:8000/devices/sensor-001/anomalies?window_start=2026-09-22T10:00:00Z&threshold=5.0'
```

```json
[
  {
    "event_id": "evt-spike",
    "event_time": "2026-09-22T10:00:46Z",
    "value": 80.2,
    "score": 134.2255
  }
]
```

- 每个结果给出超阈值事件的 `event_id`、`event_time`、`value` 与稳健 `score`，按 `event_time`、`event_id` 排序；无异常返回 `[]`。
- 当窗口 MAD 为 0（过半事件等于中位数）时，所有不等于中位数的事件均判为异常，其 `score` 为 `null`。
- 设备不存在或窗口不存在返回 `404`；窗口尚未封存（`open`）返回 `409`；`threshold <= 0` 返回 `422`。

### 16. 跨设备窗口检索（游标分页）

按窗口时间范围、状态与设备当前标签跨设备检索，结果按 `window_start`、`device_id` 升序：

```bash
# 时间范围（对 window_start 的半开区间 [start, end)）+ sealed 状态 + 设备标签
curl 'http://localhost:8000/windows/search?start=2026-09-22T00:00:00Z&end=2026-09-22T00:03:00Z&status=sealed&label_key=room&label_value=Hall-A&limit=100'

# 不带任何筛选：返回全部设备的全部窗口
curl http://localhost:8000/windows/search
```

```json
{
  "windows": [
    {
      "device_id": "sensor-001",
      "window_start": "2026-09-22T00:00:00Z",
      "window_end": "2026-09-22T00:01:00Z",
      "status": "sealed",
      "count": 3, "sum": 64.4, "avg": 21.4667, "min": 20.9, "max": 22.0,
      "correction_count": 1,
      "late_event_count": 1,
      "sealed_at": "2026-09-22T00:02:31Z"
    },
    {
      "device_id": "sensor-002",
      "window_start": "2026-09-22T00:00:00Z",
      "window_end": "2026-09-22T00:01:00Z",
      "status": "sealed",
      "count": 2, "sum": 40.0, "avg": 20.0, "min": 19.8, "max": 20.2,
      "correction_count": 0,
      "late_event_count": 0,
      "sealed_at": "2026-09-22T00:02:20Z"
    }
  ],
  "next_cursor": "eyJ2IjoxLCJ3cyI6MTc5MDA4NDgwMC4wLCJkaWQiOiJzZW5zb3ItMDAyIiwic25hcCI6MjQsImlzcyI6MTc5MDA4NTAwMC4xMjN9.8f2a..."
}
```

翻页时把筛选参数原样带上、`next_cursor` 作为 `cursor` 传回（`limit` 可调整）；没有更多数据时 `next_cursor` 为 `null`：

```bash
curl 'http://localhost:8000/windows/search?status=sealed&limit=100&cursor=eyJ2Ijox...'
```

- 翻页期间新增的窗口不会进入本次遍历（既不重复也不遗漏）；要看新增数据，不带 `cursor` 重新检索即可。
- 游标非法、被篡改、过期（默认 1 小时）或与当前筛选条件不一致返回 `400`；`end <= start`、只给 `label_value`、`limit` 越界返回 `422`。

### 17. 设备级高值告警（触发/恢复双阈值）

先设置规则（触发线 80、恢复线 70；设备须已注册，可先上报一个事件或调用 config 接口）：

```bash
curl -X PUT http://localhost:8000/devices/sensor-001/alert-rule \
  -H 'Content-Type: application/json' \
  -d '{"trigger_threshold": 80.0, "recovery_threshold": 70.0}'
```

```json
{
  "device_id": "sensor-001",
  "trigger_threshold": 80.0,
  "recovery_threshold": 70.0,
  "created_at": "2026-09-23T09:00:00Z",
  "updated_at": "2026-09-23T09:00:00Z"
}
```

- 恢复阈值不严格低于触发阈值（`recovery >= trigger`，含二者相等）或阈值为 `NaN`/`Infinity` 等非有限数时返回 `400`，**旧规则不会被覆盖**；设备不存在返回 `404`。

随后按事件值驱动状态机。以下序列演示滞回：`75`（中间区间，保持正常）→ `80`（首次越线，进入告警）→ `75`（回落中间区间，**仍告警**）→ `70`（降至恢复线，解除）：

```bash
for v in 75 80 75 70; do
  curl -X POST http://localhost:8000/ingest -H 'Content-Type: application/json' \
    -d "{\"device_id\": \"sensor-001\", \"events\": [{\"event_id\": \"v-$v-$RANDOM\", \"event_time\": \"2026-09-23T09:$(date +%S)Z\", \"value\": $v}]}"
done

curl http://localhost:8000/devices/sensor-001/alert
```

```json
{
  "device_id": "sensor-001",
  "rule": {"device_id": "sensor-001", "trigger_threshold": 80.0, "recovery_threshold": 70.0, "created_at": "...", "updated_at": "..."},
  "alerting": false,
  "alarm_count": 1,
  "recovery_count": 1,
  "last_value": 70.0,
  "last_event_id": "v-70-...",
  "last_event_time": "2026-09-23T09:00:07Z",
  "last_transition": "cleared",
  "entered_at": "2026-09-23T09:00:05Z",
  "cleared_at": "2026-09-23T09:00:07Z",
  "updated_at": "2026-09-23T09:00:07Z"
}
```

- 告警中再次上报 `>= 80` 的值不会重复增加 `alarm_count`；只有先解除（`<= 70`）后再次越线才开始下一轮。
- 对未配置规则的设备调用 `GET /devices/{id}/alert` 返回 `404`；同一事件批次重试只会得到 `duplicate`，`alarm_count`/`recovery_count` 与转换流水均不变；规则与状态在服务重启后原样保留。

### 18. 查询设备缺报区间

假设 `sensor-001` 在 `10:00`、`10:03`、`10:04` 有窗口记录，水位线已推进到 `10:04:00`（窗口 60s、允许迟到 30s，封存条件覆盖到窗口起点 `10:02`），则 `10:01`、`10:02` 两个空窗构成一段缺报：

```bash
curl 'http://localhost:8000/devices/sensor-001/missing-windows?start=2026-09-22T10:00:00Z&end=2026-09-22T10:10:00Z'
```

```json
[
  {"start": "2026-09-22T10:01:00Z", "end": "2026-09-22T10:03:00Z", "missing_windows": 2}
]
```

- `start`/`end` 均必填，且必须对齐设备窗口大小（半开区间 `[start, end)`）；`end <= start` 或边界未对齐返回 `422`，设备不存在返回 `404`，设备尚无数据或区间内无缺报返回 `[]`。
- 只统计设备最早有数据窗口的**下一个窗口**起、且水位线已越过封存条件的空窗；未封存窗口不报缺。
- 迟到事件首次填入空窗后，再次查询会自动缩短或拆分缺报段（上例若 `10:02` 被迟到事件填入，则该段缩短为 `[10:01, 10:02)` 一段）；接口纯只读，不改动任何聚合。

### 19. 每日同窗基线偏移查询

假设 `sensor-001` 在 **2026-09-17～21 日每天 08:00** 的 1 分钟窗口均值分别为 `10,11,12,13,14`，目标窗口为 **2026-09-22 08:00**（已封存），均值 `20`：

```bash
curl 'http://localhost:8000/devices/sensor-001/daily-baseline?window_start=2026-09-22T08:00:00Z&days=5&threshold=3'
```

历史中位数为 `12`，MAD 为 `|v-12| = [2,1,0,1,2]` 的中位数 `1`，故 `score = |20-12|/1 = 8 > 3`，判越界：

```json
{
  "device_id": "sensor-001",
  "window_start": "2026-09-22T08:00:00Z",
  "window_end": "2026-09-22T08:01:00Z",
  "days": 5,
  "status": "ok",
  "sample_count": 5,
  "samples": [
    {"window_start": "2026-09-17T08:00:00Z", "window_end": "2026-09-17T08:01:00Z", "offset_days": 5, "avg": 10.0},
    {"window_start": "2026-09-18T08:00:00Z", "window_end": "2026-09-18T08:01:00Z", "offset_days": 4, "avg": 11.0},
    {"window_start": "2026-09-19T08:00:00Z", "window_end": "2026-09-19T08:01:00Z", "offset_days": 3, "avg": 12.0},
    {"window_start": "2026-09-20T08:00:00Z", "window_end": "2026-09-20T08:01:00Z", "offset_days": 2, "avg": 13.0},
    {"window_start": "2026-09-21T08:00:00Z", "window_end": "2026-09-21T08:01:00Z", "offset_days": 1, "avg": 14.0}
  ],
  "target_avg": 20.0,
  "baseline_median": 12.0,
  "baseline_mad": 1.0,
  "deviation": 8.0,
  "score": 8.0,
  "threshold": 3.0,
  "breached": true
}
```

- 回看窗位**缺失或仍 open 一律跳过**，不会拿相邻日期/相邻窗口代替；有效样本不足 3 个时 `status="insufficient_samples"`，基线、分数与 `breached`（`null`）均不输出判定结论。
- 历史同窗均值全部相同（MAD=0）时：目标值相同则 `score=0`、不越界；目标值不同则 `score=null` 且 `breached=true`。
- 目标窗口的迟到修正会原位更新其均值，**再次查询立即反映最新聚合与判定**。
- `days` 仅允许 `3..30`、`threshold` 必须 `> 0`，否则 `422`；窗口秒数不能整除一天返回 `422`；设备/目标窗口不存在返回 `404`，目标窗口未封存返回 `409`。

### 20. 查询设备告警周期与总告警秒数

假设 `sensor-001` 已配置告警规则，且告警转换流水为：**09:00:05** 进入告警、**09:03:05** 解除（均为流水处理时刻 `created_at`）。查询 `[09:01:00, 10:00:00)` 时，该周期被裁剪为 `[09:01:00, 09:03:05)`，计 **125** 秒：

```bash
curl 'http://localhost:8000/devices/sensor-001/alert-episodes?start=2026-09-23T09:01:00Z&end=2026-09-23T10:00:00Z'
```

```json
{
  "device_id": "sensor-001",
  "start": "2026-09-23T09:01:00Z",
  "end": "2026-09-23T10:00:00Z",
  "episodes": [
    {"entered_at": "2026-09-23T09:00:05Z", "cleared_at": "2026-09-23T09:03:05Z", "seconds": 125.0}
  ],
  "total_seconds": 125.0
}
```

- 周期按转换流水的**自增顺序**配对 `entered`/`cleared`，起止时刻取流水处理时刻 `created_at`；迟到事件**不按 `event_time` 重排**。
- 只返回与半开区间 `[start, end)` 相交的周期；`entered_at`/`cleared_at` 给原始流水时刻，`seconds` 为**裁剪后**落在区间内的秒数；`total_seconds` 为各明细之和。
- 尚未解除的周期 `cleared_at=null`，只计至 `end` 与查询时刻的较早者（查询未来区间时不会把当前仍在告警的周期计到 `now` 之后）。
- `start`/`end` 必填且 `end > start`，否则 `422`；设备或告警规则不存在返回 `404`；无相交周期返回 `{"episodes": [], "total_seconds": 0}`。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/ingest` | 按设备提交事件批次（幂等） |
| `PUT` | `/devices/{device_id}/config` | 创建/更新设备窗口与允许迟到配置 |
| `GET` | `/devices` | 设备列表，支持 `?label_key=&label_value=` 按当前标签筛选 |
| `GET` | `/devices/{device_id}` | 设备水位线与统计 |
| `PUT` | `/devices/{device_id}/labels` | 全量替换设备标签（≤20 个键值对，键名规范化，版本 +1，支持 `if_version` 乐观并发，冲突 409） |
| `GET` | `/devices/{device_id}/labels` | 设备当前标签与版本（从未写入为 version 0） |
| `GET` | `/devices/{device_id}/windows` | 各时间窗聚合值、状态、修正次数（支持 `?status=open\|sealed`） |
| `GET` | `/devices/{device_id}/corrections` | 迟到数据修正记录 |
| `GET` | `/windows/search` | 跨设备窗口检索：按时间范围（`start`/`end` 半开区间）、`status`、设备当前标签筛选，按 window_start、device_id 升序，绑定筛选条件的稳定游标分页（`limit`/`cursor`，非法或过期游标 400） |
| `GET` | `/devices/{device_id}/anomalies?window_start=&threshold=3.5` | 已封存窗口事件的中位数/MAD 稳健异常检测（只读；窗口不存在 404，未封存 409，threshold≤0 为 422） |
| `GET` | `/devices/{device_id}/missing-windows?start=&end=` | 连续缺报区间查询（只读；start/end 必填且对齐窗口大小，未对齐或 end≤start 为 422，设备不存在 404，无数据返回空列表） |
| `GET` | `/devices/{device_id}/daily-baseline?window_start=&days=&threshold=` | 每日同窗基线偏移查询（只读；按 UTC 逐日同格回看已封存窗口，中位数/MAD 打分，缺失/未封存跳过不替代，有效样本 <3 返回 insufficient_samples，MAD=0 同值记 0/异值 score=null 且越界；days 3..30、threshold>0、窗口秒数不整除一天为 422，目标窗口不存在 404、未封存 409） |
| `PUT` | `/devices/{device_id}/alert-rule` | 设置/替换高值告警双阈值（trigger 必须严格高于 recovery，均为有限数；非法 400 且旧规则保留，设备不存在 404） |
| `GET` | `/devices/{device_id}/alert` | 查询当前告警状态与生效规则（无规则 404；状态持久化、重启不丢） |
| `GET` | `/devices/{device_id}/alert-episodes?start=&end=` | 告警周期与总告警秒数查询（只读；按转换流水自增顺序配对 entered/cleared，以流水 created_at 为起止，裁剪到 [start,end)，未解除周期计至 end 与查询时刻较早者；start/end 必填且 end>start 否则 422，设备或规则不存在 404，无相交周期返回空明细与 0） |
| `POST` | `/groups` | 创建设备组（固定成员、sum/avg、窗口配置一致校验） |
| `GET` | `/groups/{group_id}` | 组信息与成员列表 |
| `GET` | `/groups/{group_id}/windows` | 组窗口汇总列表（支持 `?status=pending\|ready&window_start=...`） |
| `GET` | `/groups/{group_id}/windows/{window_start}` | 查询单个组窗口 |
| `GET` | `/groups/{group_id}/windows/{window_start}/pending` | pending 成员明细（`missing`/`open`；ready 返回空列表） |
| `GET` | `/groups/{group_id}/windows/{window_start}/revisions` | 组窗口修订审计列表（不可变快照，含初始化 baseline） |
| `GET` | `/groups/{group_id}/windows/{window_start}/revisions/diff?from_revision=&to_revision=` | 两版本 diff：按成员顺序返回变化成员的前后贡献与数值差（非法版本 422，不存在 404） |

## 项目结构

```
app/
  main.py        # FastAPI 应用、遥测聚合、设备标签（版本化/乐观并发）、设备组同步窗口汇总与修订审计快照、跨设备窗口检索（稳定游标分页）、设备级高值告警（双阈值滞回状态机）、告警周期查询（流水配对/区间裁剪/总秒数）、设备缺报区间查询、每日同窗基线偏移查询逻辑
  db.py          # SQLite 连接、建表（含标签表、append-only 审计表/触发器、告警规则/状态/转换流水表）、旧库迁移与事务会话
tests/
  test_api.py       # 端到端测试（水位线/封存/去重/迟到修正/持久化）
  test_labels.py    # 设备标签测试（规范化/重复键/版本递增/乐观并发/标签筛选/旧库迁移）
  test_groups.py    # 设备组测试（pending/ready、修正升版、重试幂等、成员明细）
  test_revisions.py # 修订审计测试（快照留档、diff、422/404、不可变性、旧库 baseline 补档）
  test_anomalies.py # 封存窗口稳健异常检测测试（中位数/MAD 打分、MAD=0、404/409/422、只读）
  test_windows_search.py # 跨设备窗口检索测试（时间范围/状态/标签筛选、游标分页稳定性、400/422、只读）
  test_alerts.py    # 设备级高值告警测试（滞回转换/边界取等、非法阈值不覆盖旧规则、重试幂等、迟到驱动、重启持久化、唯一约束兜底）
  test_alert_episodes.py # 告警周期查询测试（流水配对/created_at 起止/半开区间裁剪/未解除计至 end 与查询时刻较早者/迟到不重排/422/404/空结果/只读）
  test_missing_windows.py # 设备缺报区间查询测试（缺报段计算/封存条件裁剪/半开边界/迟到填空拆分/422/404/只读）
  test_daily_baseline.py # 每日同窗基线偏移查询测试（UTC 同格回看/仅取已封存样本/不替代缺失/中位数/MAD 打分/样本不足/MAD=0 退化/迟到修正再查/整除校验/参数校验/404/409/只读）
Dockerfile
docker-compose.yml
requirements.txt
requirements-dev.txt
```
