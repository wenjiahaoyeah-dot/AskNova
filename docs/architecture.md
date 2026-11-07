# AskNova 架构与设计

## 组件关系

FastAPI 管理组件生命周期；Orchestrator 负责识别与路由；Agent 通过工具调用完成资料查询和角色内处理；Memory 负责跨轮上下文；Evaluator 和 Monitor 分别观察离线回复质量与在线运行表现。

```text
api/main.py
  ├─ core/intent_recognizer.py         意图、实体与紧急度
  ├─ agents/agent_orchestrator.py      路由、Agent Loop、并行、合成
  │    ├─ agents/tools.py              本地角色工具 / 共享 RAG 入口
  │    ├─ core/skill_loader.py         匹配并注入角色处理规范
  │    └─ tooling/mcp_client.py        远端工具适配
  ├─ tooling/tool_manager.py          缓存、熔断、超时、改写和重排
  │    └─ tooling/knowledge_base.py    文档、向量与关键词混合检索
  │         └─ hybrid_retrieval.py     BM25 / RRF
  ├─ memory/conversation_memory.py     Redis / ChromaDB 分层记忆
  ├─ evaluation/evaluator.py           意图与对话质量评测
  └─ monitor/performance_monitor.py    在线统计、路由反馈与指标
```

## 1. 三路意图识别

LLM 使用意图枚举、格式要求与 Few-shot 示例产生分类及分数；Embedding 分支匹配意图示例；Pattern 分支通过规则与关键词生成候选。启用 Embedding 时固定融合权重为 LLM 0.7、Embedding 0.2、Pattern 0.1；禁用时为 LLM 0.85、Pattern 0.15。融合包含领域累计、领域内选择和细化逻辑，不能只理解成对三种置信度求平均。

识别输出包含意图、领域、置信分数、实体、紧急度和来源分数。置信分数没有经过概率校准；不能等同于准确率。实体主要通过程序规则提取。意图模板、结果缓存以及学习接口新增样本在进程中维护。

意图 Embedding 可选在线 BGE-M3；远程不可用或没有配置 Key 时，整个匹配回到同一空间的本地字符 n-gram 哈希向量，避免不同模型的向量混用。该分支与知识库默认 Embedding 分开。

代码：[intent_recognizer.py](../core/intent_recognizer.py)、[intent_embedding.py](../core/intent_embedding.py)。

## 2. 主辅 Agent 编排

Orchestrator 根据意图、关键词、实体及紧急度选择处理路径。需要澄清时直接回复；需要人工升级时生成交接信息；单领域由一个专业 Agent 处理；复合问题可执行主辅 Agent 并行，再由 Composer 合并回复。

每个 Agent 接收结构化角色输入，以及角色契约和匹配 Skills。模型通过 Tool Calling 返回工具名和参数，程序再次校验工具是否允许、参数是否符合 Schema，然后执行。结果以 `tool_result` 返回给模型。Agent Loop 最多进行三次模型调用，超出预算会显式报告失败。

角色包含 General、Technical、Billing、Escalation。专业 Agent 执行失败通常退回 General；已经尝试不安全副作用工具时不自动重复执行。当前内置工具没有真实订单、支付或工单服务连接。

在线表现评分综合成功率与延迟，再乘 Monitor 惩罚项。高惩罚可使专业 Agent 暂时不可用，冷却后允许恢复探测。此机制影响实例评分和可用性，不会自动学习意图融合系数。

代码：[agent_orchestrator.py](../agents/agent_orchestrator.py)、[tools.py](../agents/tools.py)。

## 3. 知识构建与混合检索

导入支持 JSON 文档数组或 TXT/Markdown 文件。标题与正文经过校验，以显式文档 ID 或标题确定身份，正文按句子边界和最多 500 字符切片，无 overlap。每片保留正文、标题、文档身份、正文版本哈希、片段序号和片段总数。

Chroma Python 客户端使用默认 Embedding 函数生成片段向量；服务端保存向量、正文与元数据。更新使用 upsert 并清理不再存在的旧片段。Chroma collection 默认距离度量没有被改为显式 cosine，因此 `1 - distance` 不能一概解释为余弦相似度。

关键词分支在首次检索时从 Chroma 分页构建内存 BM25 索引：标题加正文，中文单字与双字片段，英文、数字和错误码按词；参数为 k1=1.5、b=0.75。每个检索查询的两路召回并行执行，候选预算为 `max(20, top_k * 4)`，向量路还受库内片段数限制。

RRF 按真实片段 ID 合并两路排名：

```text
RRF(d) = Σ 1 / (60 + rank_i(d))
```

两路等权，只使用排名，不直接相加 BM25 与向量原始分数。例如某片段向量排名 2、BM25 排名 1，融合分数为 `1/62 + 1/61`。结果中的 `score` 与 `rrf_score` 均表示该排名分数，`retrieval_sources` 说明命中来源。

Agent 的共享 RAG 工具会保留原始查询，并生成最多三条改写查询。各查询分别混合召回，再按内容与文档身份信息计算哈希去重。候选超过最终 top_k 时调用 LLM 排序；失败时保持既有候选顺序。RRF 在每个查询内执行，最终重排处理跨查询候选，不进行跨查询的第二次 RRF。

BM25 单路故障时可以使用向量路，向量单路故障时可以使用关键词路；两者都需要 Chroma 中的资料，不能保证 Chroma 完全不可用时继续检索。当前进程导入会失效 BM25 索引；其他进程更新由数量变化或 60 秒刷新周期感知。查询结果另有工具层 TTL 缓存，需要同时考虑其新鲜度。

代码：[knowledge_base.py](../tooling/knowledge_base.py)、[hybrid_retrieval.py](../tooling/hybrid_retrieval.py)、[tool_manager.py](../tooling/tool_manager.py)。

## 4. MCP 与工具权限

外部 MCP 使用官方 SDK 的 ClientSession，支持 Streamable HTTP 与 stdio。连接后 initialize、分页 list_tools、过滤白名单和只读标记，将工具 Schema 适配成 AgentToolSpec，再通过工具管理器执行 call_tool。

当前示例连接 Microsoft Learn 文档 MCP，只向 General、Technical 开放搜索和读取文档工具；没有绑定用户的订单或财务账号。外部工具目录变化会触发刷新，断线后移除工具并重连。读取 `readOnlyHint` 是对服务声明的过滤，不是对远端实际行为的独立验证。

本地使用 FastMCP 暴露 `search_local_knowledge` 工具和 `asknova://knowledge/stats` 资源，入口 `/mcp/`。本地 MCP 查询直接执行混合检索，不包含 Agent 共享 RAG 的外层查询改写和 LLM 重排。

工具选择由 LLM 在程序提供的工具集合中完成；MCP 提供通信协议，工具管理器提供运行治理。本地 Python 工具调用不需要经过 MCP 网络连接。

代码：[mcp_client.py](../tooling/mcp_client.py)、[mcp_server.py](../tooling/mcp_server.py)、[mcp_servers.json](../config/mcp_servers.json)。

## 5. 记忆与并发一致性

Redis 保存近期原始消息、会话摘要、领域状态、幂等回执和脱敏轨迹；Chroma 的独立 collection 保存情景摘要和用户画像，业务知识库使用另一个 collection。上下文读取时组合摘要、相关历史、画像与近期消息供模型使用。

消息达到压缩阈值后，较早部分由模型生成摘要并保留最近五条原始消息。压缩在提交前通过 WATCH 检查快照，生成失败时保留原消息。当前会话摘要与近期原文分键保存。

请求由进程锁与 Redis 租约协调；消息、状态与响应回执通过 Redis 事务提交。同一 `client_request_id` 可以重放已有结果，相同 ID 配不同消息返回冲突。这些机制降低重复执行与错序风险，不构成跨所有外部服务的分布式事务。

代码：[conversation_memory.py](../memory/conversation_memory.py)、[concurrency.py](../core/concurrency.py)、[conversation_state.py](../core/conversation_state.py)。

## 6. 评测与运行追踪

意图评测计算标签准确率和 Macro-F1；对话用例调用 Orchestrator，LLM-as-a-Judge 根据问题、回复、历史及可选参考资料，从四个维度评分。还可设置预期 Agent、意图、必要工具和实体进行确定性检查。

基线比较要求相同 suite_id，包含用例和 Judge 配置。评分失败样本排除均值，本次不更新基线；缺少同配置有效基线时跳过比较。评测任务状态暂存在进程中，服务重启后不可恢复任务。

工具轨迹记录调用输入、成功状态、耗时、缓存命中、重排及错误；做脱敏后保留近期记录，并可写入 Redis。Prometheus 提供请求计数、Agent 与工具成功率和延迟指标。这里是请求级工具轨迹，不是跨服务分布式 tracing 系统。

代码：[evaluator.py](../evaluation/evaluator.py)、[jobs.py](../evaluation/jobs.py)、[performance_monitor.py](../monitor/performance_monitor.py)。
