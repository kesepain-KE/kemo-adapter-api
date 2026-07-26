# 每日调用统计

网关将每次真实 Provider 执行的脱敏统计写入 `daily/YYYY/MM/YYYY-MM-DD.sqlite3`。
日期边界默认使用 `Asia/Shanghai`，数据库中的时间戳使用 UTC。

- 不保存提示词、响应正文、Authorization、明文密钥或厂商原始错误。
- Token 字段只接受 Provider `usage.py` 归一化后的 `Usage`；未知值保留为 `NULL`。
- 缓存命中率只计算同时具有精确 `input_tokens` 和 `cached_input_tokens` 的样本。
- 幂等重放只增加 `replay_count`，不会增加真实调用数。
- 数据库写入失败不会中断模型响应，可通过管理端统计 API 查看存储健康状态和丢弃数。

`daily/` 是运行数据，不纳入 Git 版本管理。修改统计代码后需要重启网关。
