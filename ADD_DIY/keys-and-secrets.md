# 密钥与敏感配置操作

## 存储位置

- 无需重启的网关调用方 Token：`api/keys.json`；
- 无需重启的厂商 API Key：`providers/<provider_id>/secrets.json`；
- 启动/应急配置：`.env` 中的 `GATEWAY_API_KEY`、`GATEWAY_API_KEYS_JSON` 和
  `PROVIDER_SETTINGS_JSON`，修改后必须重启；
- 生产环境可由 Secret Manager 原子更新上述运行时密钥文件；
- `.env`、`api/keys.json` 和 `providers/*/secrets.json` 已被 `.gitignore` 排除。

真实密钥不得写入 `.env.example`、Provider 源码、测试 Fixture、Markdown、Shell 历史或命令参数。

## 创建或轮换调用密钥

1. 生成密码学安全的随机 Token；
2. 将 Token 写入 `api/keys.json` 或由 Secret Manager 更新该文件；
3. 为 Token 绑定明确的 `tenant_id`、`subject_id` 和最小 scopes；
4. 新旧 Token 短暂并行，通过认证测试后撤销旧 Token；
5. 使用原子替换写入；下一次认证请求自动加载，无需重启；
6. 最终报告只记录 key id/后四位和操作结果，不回显完整 Token。

## 修改厂商密钥

1. 确认目标 `provider_id` 和环境；
2. 在厂商控制台创建新 Key，不要立即撤销旧 Key；
3. 更新 `providers/<provider_id>/secrets.json` 或对应 Secret Manager；
4. 执行能力查询和最小非流式请求；
5. 确认新 Key 生效后撤销旧 Key；
6. 检查日志、错误对象和 `provider_raw` 中没有泄漏。

智能体不得在未获得明确授权时创建付费厂商资源、撤销仍在使用的 Key 或修改生产租户权限。
修改任何 `.env` 环境变量后必须重启，不能把环境变量误报为已热加载。
