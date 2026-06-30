# 链路追踪与日志关联增强计划

## 结论

这次 PR 的主旨应当是 **链路追踪**，不是单纯的 Langfuse 接入，也不是单纯的日志格式调整。

建议把 PR 写成两个实现部分：

1. **全局注入 DeerFlow 关联 ID**：在一次 agent runtime 中生成并传播同一个新的 UUID 形式的 `deerflow_trace_id`，让主 agent、subagent、memory、suggestion 产生的 Langfuse trace 都能通过 `metadata.deerflow_trace_id` 查询到。
2. **增强日志关联能力**：把同一个关联 ID 注入已有业务执行路径的日志上下文，日志输出中使用 `trace_id` 字段承载这个值，方便从日志侧追踪一次 agent runtime 的完整执行过程。

这里的 `deerflow_trace_id` 是 DeerFlow 自己新生成的关联 ID，不是 Langfuse trace id，也不是某个 APM 厂商的 trace id，更不是 DeerFlow 现有的 run id。Langfuse trace 仍然可以保持现有拆分模型：主 agent、subagent、memory、suggestion 可以各自拥有独立的 Langfuse trace，只是它们共享同一个 DeerFlow metadata key。

## 为什么这样更清晰

现有文档把事情拆成“前置 Langfuse PR”和“后续日志 PR”，容易让读者以为这是两个互相独立的方向。更准确的抽象是：

- `deerflow_trace_id` 是源头和主线。
- Langfuse metadata 是它的一个可观测性落点。
- 日志字段是它的另一个可观测性落点。
- 其他接口日志增强也应围绕这个 ID 展开，而不是引入另一套关联字段。

这样做的好处是查询路径非常直接：

- 在 Langfuse 中按 `metadata.deerflow_trace_id = <id>` 查询同一次 agent runtime 的全部相关 trace。
- 在日志系统中按 `trace_id = <id>` 查询同一次 agent runtime 的全部相关日志。
- 在 DeerFlow 自身数据中仍然按 `thread_id` / `run_id` 查询业务对象。

## 当前状态

当前 Langfuse 接入已经具备基础能力：

- `langfuse_session_id` 对应 LangGraph `thread_id`。
- `langfuse_user_id` 对应有效用户。
- `langfuse_trace_name` 区分 `lead-agent`、`subagent:<name>` 等 trace 名称。
- `langfuse_tags` 可写入环境和模型标签。

但还缺少一个跨组件共享的 DeerFlow 关联 ID：

- 主 agent trace 和 subagent trace 可以归到同一个 session，但缺少稳定的一次 runtime 关联字段。
- memory / suggestion 是独立的 LLM 调用时，无法稳定判断它们来自哪一次主 agent runtime。
- subagent 代码里已有局部 `trace_id` 日志前缀，但它还不是统一的、结构化的 DeerFlow runtime 关联 ID。
- gateway 日志目前主要依赖 `logging.basicConfig(...)` 和顶层 `log_level`，没有统一的 `trace_id` LogRecord 字段。

## 目标语义

`deerflow_trace_id` 表示一次用户触发的 agent runtime 及其派生任务。它应当由 DeerFlow 在 runtime 边界生成，使用新的 UUID；调用方只用它做查询和关联，不应把它理解为某个已有业务对象 ID。

生成规则建议：

1. lead agent runtime 入口为每次新 runtime 生成一个新的 UUID，写作 `deerflow_trace_id`。
2. 不复用、截断或派生 DeerFlow 现有 `run_id` 作为 `deerflow_trace_id`。
3. 同一次 runtime 内派生出的 subagent、memory、suggestion 复用同一个 `deerflow_trace_id`。
4. 没有父 runtime 上下文的独立辅助请求，不做 request 级 `deerflow_trace_id` 兜底；它不应参与这次 runtime 关联。
5. 一旦生成，在同一次 runtime 内不可再替换。

传播规则建议：

- **lead agent**：在 `run_agent` / `DeerFlowClient.stream` 的 graph root config 中注入 `deerflow_trace_id`，同时放入 runtime context，供 middleware 和 tool 读取。
- **subagent**：`task_tool` 从父 runtime context/metadata 读取 `deerflow_trace_id`，传给 `SubagentExecutor`，并写入 subagent graph 的 Langfuse metadata 与日志上下文。
- **memory**：`MemoryMiddleware` 入队时捕获当前 trace context；`MemoryUpdater` 后台执行时复用该 context，并在 `memory_agent` LLM 调用中写入 metadata。
- **suggestion**：suggestion 请求当前是独立 HTTP 请求。要稳定挂到上一轮主 run，需要前端或调用方把最近一次 AI message/run 对应的 `deerflow_trace_id` 传给 `/api/threads/{thread_id}/suggestions`；如果拿不到父上下文，本次 PR 不为它生成 request-level trace id，也不把它纳入某个具体主 agent run 的关联链路。

## Metadata 字段

保持字段瘦身，第一版只写一个 DeerFlow 关联字段：

- `deerflow_trace_id`：同一次 agent runtime 共享的关联 ID。

不重复写入 Langfuse 已经承载的 session/thread 维度；DeerFlow 自身数据中也仍然按 `thread_id` / `run_id` 查询业务对象。第一版的目标不是补齐完整业务关系图，而是先建立跨独立 Langfuse trace 的最小查询主键。

示例：

```json
{
  "deerflow_trace_id": "019c2f4a-8c1d-7ad0-9ef5-0c1a7e8a9f12"
}
```

Langfuse 保留自己的 trace id。DeerFlow 只把上述字段写入 Langfuse metadata，用于跨独立 trace 查询。

后续如果需要更完整的 run/task 关系，可以再考虑补充：

- `deerflow_root_run_id` / `deerflow_parent_run_id`：用于表达主 run 与派生任务的层级关系。这里应该使用 DeerFlow 现有的 `run_id` 语义，而不是把 `run_id` 复用成 `deerflow_trace_id`。
- `deerflow_task_id`：用于表达 subagent tool call 或后台任务粒度，方便在同一次 runtime 内继续下钻。

这些字段不进入第一个 PR 的实现范围。

## 日志增强

日志侧的目标是让每一条关键日志都能按同一个 ID 查询。

这部分建议新增一层独立但轻量的 logging config，用它接管当前散落在
`backend/app/gateway/app.py` 里的 `logging.basicConfig(...)` 职责。这里的“接管”不等于默认改变日志行为：默认配置下应复刻现有输出格式和 log level 处理；只有显式打开增强日志时，才安装 trace context filter，并把 `trace_id` 加入日志 format。

日志输出字段第一版只保留一个：

- `trace_id`：值等于当前 `deerflow_trace_id`。

第一版不提供 `context_fields` allowlist，也不把 `thread_id`、`run_id`、`component`、`user_id` 放入日志 schema。这样可以避免业务阶段字段在尚未产生时被提前猜测或伪造，也避免用户标识类字段默认进入日志输出。后续如果确实需要业务字段，应作为独立增强重新讨论字段语义、生命周期和隐私边界。

HTTP 响应头建议：

- 只有 `logging.enhance.enabled=true` 时，才考虑在 HTTP 响应头写入 `X-Trace-Id`；默认关闭时不增加响应头，保持现有 HTTP 行为不变。
- 在增强日志开启且请求已经生成或承载 `deerflow_trace_id` 时，在响应头写入 `X-Trace-Id: <deerflow_trace_id>`。
- `X-Trace-Id` 只暴露同一个 DeerFlow trace correlation id，方便调用方把前端/客户端错误、Langfuse trace 和日志查询串起来。
- 如果某个普通请求没有 DeerFlow runtime trace，上述响应头可以省略；不要为了响应头而生成 request-level trace id。

日志上下文的核心边界应当是：

- `ContextVar` 是日志关联上下文的唯一运行时载体；logging filter 只负责把当前 `ContextVar` 内容注入 `LogRecord`。
- `trace_id` 必须由明确知道 DeerFlow runtime 语义的边界显式绑定，例如 `start_run`、`run_agent`、`DeerFlowClient.stream`、`SubagentExecutor`、`MemoryMiddleware/MemoryUpdater`、suggestion handler。
- 不做 request 级日志兜底：HTTP middleware 不生成请求级 `trace_id`，也不新增通用 request completion 日志。
- `thread_id`、`run_id` 等业务字段不进入第一版日志输出；它们即使在代码中存在，也不应从请求路径或其他外层信息猜测后注入日志。
- 如果某条请求没有父 runtime 上下文，本次 PR 不为日志关联额外生成 trace id。

实现建议：

1. 新增轻量 logging config，例如 `deerflow.logging_config.configure_logging(...)`。
2. 由 `configure_logging(...)` 统一替代 gateway 当前的 `basicConfig(...)` 入口。
3. 新增基于 `ContextVar` 的 observability context，只保存当前 `trace_id`。
4. 新增 logging filter，把 `trace_id` 注入 `LogRecord`，并保证字段缺失时有默认值，避免 formatter 报错。
5. 在 agent runtime 边界绑定 context，并通过 context propagation 覆盖 subagent、memory、suggestion 的执行路径。
6. 当且仅当 `logging.enhance.enabled=true` 且请求已经有 `deerflow_trace_id` 时，对 HTTP 响应写入 `X-Trace-Id`；没有 DeerFlow trace 的普通请求不生成兜底 trace，也不写该响应头。
7. 不新增 gateway 通用请求日志；已有业务日志如需关联，只从 `ContextVar` 读取已绑定的 `trace_id`。
8. 日志 formatter 可以继续支持 text；如果提供 JSON formatter，应当是可选能力。

默认行为应保持兼容：

```yaml
log_level: info

logging:
  enhance:
    enabled: false
    format: text
```

`enabled: false` 时必须保持当前 `basicConfig + apply_logging_level` 行为等价，不安装 trace context filter，不改 formatter，不增加 `trace_id` LogRecord 字段依赖，不额外安装重复 handler，也不改变既有日志 format。`deerflow_trace_id` 仍然可以为 Langfuse metadata 生成和传播，但不会影响日志输出。

`enabled: true` 时安装 context/filter/formatter，并输出唯一的 `trace_id` 字段。text 格式可以类似：

```text
%(asctime)s - %(name)s - %(levelname)s - [trace_id=%(trace_id)s] - %(message)s
```

JSON 格式则把 `trace_id` 输出为独立字段。第一版不允许用户选择额外日志上下文字段。

## PR 范围

建议这个 PR 以“链路追踪增强”为标题，内部拆成两个提交或两个实现块，而不是在文档中拆成两个独立方向。

范围：

1. 定义 DeerFlow trace correlation helper，生成 UUID 并构建最小 metadata。
2. 在 lead agent runtime 边界生成/注入新的 `deerflow_trace_id`。
3. 将同一个 ID 传递到 subagent、memory、suggestion。
4. 将 DeerFlow correlation metadata 写入 Langfuse trace metadata。
5. 新增轻量 logging config，统一接管现有 `basicConfig` 入口，并保持默认输出不变。
6. 新增日志 context/filter，让增强日志开启时输出包含 `trace_id`。
7. 开启日志增强时，对已生成或承载 `deerflow_trace_id` 的 HTTP 响应写入 `X-Trace-Id`。
8. 保持默认日志行为不变，不引入额外厂商后端。

不做：

- 不把所有 Langfuse trace 合并成一条 trace。
- 不复用或改写 Langfuse 自己的 trace id。
- 不复用或派生 DeerFlow 现有 `run_id` 作为 `deerflow_trace_id`。
- 不在第一版实现 run/task 关系 metadata；这些可以作为后续增强。
- 不把 `thread_id`、`run_id`、`component`、`user_id` 等业务字段加入第一版日志 schema。
- 不做 request 级 trace/logging 兜底。
- 不新增 gateway request completion 日志或通用结构化请求日志。
- 不从 URL/path 正则解析 `thread_id`、`run_id` 等业务字段。
- 不接入 Kafka、SkyWalking、ELK 或特定 APM schema。
- 不强制 JSON 日志。
- 不让日志增强默认改变已有部署输出。

## 验收标准

- 启用 Langfuse 后，一次主 agent runtime 产生的 lead agent、subagent、memory、suggestion trace 都能用同一个 `metadata.deerflow_trace_id` 查到。
- 日志增强开启后，同一次 runtime 的关键日志都包含相同 `trace_id`，且不输出其他业务上下文字段。
- 开启日志增强后，对已生成或承载 `deerflow_trace_id` 的 HTTP 响应，客户端能从 `X-Trace-Id` 读取同一个关联 ID。
- Langfuse disabled 时，业务行为不变；日志增强 disabled 时，现有日志输出不变。
- suggestion 如果没有收到父 run/trace 上下文，本次 PR 不为它生成 request-level trace，也不把它声明为某次主 agent run 的派生 trace。
- 测试覆盖最小 metadata 构建、lead/subagent/memory/suggestion 传播、logging filter 注入，以及默认关闭时不重复 handler。
