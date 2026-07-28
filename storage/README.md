# 每日调用统计

网关将每次真实 Provider 执行的脱敏统计写入 `daily/YYYY/MM/YYYY-MM-DD.sqlite3`。
日期边界默认使用 `Asia/Shanghai`，数据库中的时间戳使用 UTC。

- 不保存提示词、响应正文、Authorization、明文密钥或厂商原始错误。
- Token 字段只接受 Provider `usage.py` 归一化后的 `Usage`；未知值保留为 `NULL`。
- 缓存命中率只计算同时具有精确 `input_tokens` 和 `cached_input_tokens` 的样本。
- 幂等重放只增加 `replay_count`，不会增加真实调用数。
- 数据库写入失败不会中断模型响应，可通过管理端统计 API 查看存储健康状态和丢弃数。

`daily/` 是运行数据，不纳入 Git 版本管理。修改统计代码后需要重启网关。

## 多模态 Asset

`assets/` 保存 `/assets` 上传的输入媒体和 Provider 登记的生成产物。每个 Asset 的公开描述只
包含稳定 ID、用途、文件名、MIME、大小、SHA-256、状态和有效期；本地 `content.bin` 路径只允许
存储层和绑定当前 tenant/subject 的 `RequestContext.assets` 使用。

- 图片、音频、视频和普通文件分别应用环境变量中的大小上限；
- 输入按当前 tenant + subject + Idempotency-Key 隔离与去重；
- 生成媒体必须先登记为 `purpose=output`，公开响应不得泄露本地路径；
- 过期检查会在访问时返回 410，后台清理任务会回收过期内容；
- `assets/` 是部署数据，不纳入 Git，也由更新器保护。
