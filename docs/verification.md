# 导出检查记录

本记录针对 2026-11-07 整理的 AskNova 核心代码展示版。

| 检查 | 结果 |
|---|---|
| Python 源码语法解析 | 39 个文件通过 AST 解析 |
| 混合检索离线回归 | 16 项通过 |
| 文档导入回归 | 4 项通过 |
| Markdown 相对链接 | 通过 |
| 品牌与命名 | 已统一为 AskNova / asknova / ASKNOVA |
| 发布内容检查 | 未包含真实 `.env`、本地密钥、聊天数据、数据库或原仓库 Git 历史 |
| 变更空白检查 | 通过 |

混合检索测试覆盖中文片段、英文与错误码匹配、跨路融合、候选补召回、文档更新、索引缓存与分页、并行执行和单路降级。导入测试覆盖重复导入、缩短文档后的旧片段清理、同标题不同 ID 和超长句切片。

```bash
python -m unittest discover -s tests -p test_hybrid_retrieval.py -v
```

该轮检查使用 Python 3.9 的标准库执行上述离线回归和语法解析；项目运行环境仍要求 Python 3.12。完整 pytest、Docker 构建、真实 ChromaDB、线上模型、Microsoft Learn MCP 连接和 Recall/MRR 指标未在本轮验证。完整开发依赖与现有回归测试已随核心代码保留，可按 README 在 Python 3.12 环境继续验证。
