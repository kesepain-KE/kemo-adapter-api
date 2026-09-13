# 离线教学样例：能执行，不自动部署

这里的模型、URL 和 Token 全是示例。没有任何厂商 Client，不加载 `providers/`、不访问网络、
不读 `.env` 或真实 secrets。不要把这个目录复制成一个 Provider。

| 文件 / 函数 | 用途 | 不能证明什么 |
| --- | --- | --- |
| `task-card.md` | 开工前确认操作范围 | 不代表用户已经授权重启或付费调用 |
| `model_workflows.py:capability_example` | 九种 Kemo 操作的合法能力声明 | 目标厂商有该能力 |
| `model_workflows.py:request_example` | 九种配对的完整 Kemo 请求 | 示例媒体 URL 可访问 |
| `model_workflows.py:add_verified_operations` | 单模型增量增加操作，保留其他字段 | 媒体端点/输入输出转换已实现 |
| `model_workflows.py:reasoning_example` | 五档折叠为模拟三档 | 所有上游接受 thinking_level |
| `model_workflows.py:reasoning_payload_example` | 显式映射与非法档位拒绝 | 推理开关、回放状态自动完成 |
| `model_workflows.py:retrieval_example` | Embedding/Rerank 配对能力与请求 | 模拟维度/批量数字是上游真实限制 |
| `gateway-keys.json.example` | 网关调用密钥和白名单结构 | 该示例 Token 可用于生产 |

在网关根目录执行（Windows/Linux 相同）：

```sh
python -m tests --suite templates -q
```

这条命令验证教学样例与模板，不会验证部署端全部厂商。
要检查一个实际 Provider，另外运行 `python -m pytest -q providers/<provider_id>/test_contract.py`。
新包内的 `catalog_contract.py` 可随包复制；运行时不要从 `template.examples` 导入教学逻辑。

其余测试统一从 [tests/README.md](../../tests/README.md) 选择套件；共享测试构造器只放在
`tests/support/`，不要从另一个 `test_*.py` 导入。
