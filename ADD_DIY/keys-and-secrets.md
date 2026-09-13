# 密钥与敏感配置操作

先看用户指的是哪种密钥。上游厂商密钥用于“网关 → 厂商”，网关调用 Token 用于
“智能体 → 网关”，Web 登录和 STATUS_TOKEN 又是另外的凭据。不能互换，也不要全部同时修改。

## 存储位置

- 无需重启的网关调用方 Token：`api/keys.json`；
- 无需重启的厂商 API Key：显式保存在 `providers/<provider_id>/secrets.json`；标准持久化
  格式固定为 `api_keys` 数组；
- 启动/应急配置：`.env` 中的网关调用方鉴权配置，修改后必须重启；这些参数不是 Provider
  上游密钥的存储位置；
- 网关对外展示地址：`.env` 中的 `GATEWAY_BASE_URL`，只用于网页复制，不改变监听地址；
- 生产环境可由 Secret Manager 原子更新上述运行时密钥文件；
- `.env`、`api/keys.json` 和 `providers/*/secrets.json` 已被 `.gitignore` 排除。

真实密钥不得写入 `.env.example`、Provider 源码、测试 Fixture、Markdown、Shell 历史或命令参数。

## 网页掩码不是实际密钥

网关配置页的认证与密钥、Provider 密钥池使用后端生成的前五后三掩码，例如
`abcde…xyz`（假数据）；长度不超过 10 的短值和空值仍为 `***`。多把密钥分别显示。
这只用于用户在管理界面辨认凭据，不代表授权智能体打印凭据片段。
新增 Provider 应复用核心统一路由的脱敏状态，不自行返回 api_key 或 Headers。

- 不把 `abcde…xyz`、`***` 或网页状态当作真实密钥写回配置；
- 不让前端获取完整值后再截取，Provider 私有 JSON 和敏感请求头也不作字符串首尾预览；
- 掩码可能相同，修改时以唯一 `key_id` 和明确目标为准，不能只匹配首尾片段；
- `STATUS_TOKEN` 的只读接口不因管理界面增加预览而获得密钥查看权限。

## 创建或轮换调用密钥

1. 生成密码学安全的随机 Token；
2. 将 Token 写入 `api/keys.json` 或由 Secret Manager 更新该文件；
3. 为 Token 绑定明确的 `tenant_id`、`subject_id` 和最小 scopes；
   `allowed_models=null` 表示允许该 scope 下全部已启用模型，空数组表示全部禁止；
4. 新旧 Token 短暂并行，通过认证测试后撤销旧 Token；
5. 使用原子替换写入；下一次认证请求自动加载，无需重启；
6. 最终报告只记录 key_id 和操作结果，不回显 Token 原文或片段。

## 修改厂商密钥

标准文件结构：

```json
{
  "api_keys": [
    {"key_id": "primary", "api_key": "上游密钥 A", "enabled": true},
    {"key_id": "backup-1", "api_key": "上游密钥 B", "enabled": true}
  ]
}
```

旧 `api_key`/`api_key_id` 字段仅用于读取迁移；管理端写入时必须删除旧字段，只保留
`api_keys`。不得把 Provider 密钥迁移到 `.env` 或 `PROVIDER_SETTINGS_JSON`。密钥池允许
追加和删除单个上游密钥，但每个 Provider 至少必须保留一个密钥；最后一个密钥只能替换，
或先追加新密钥后再删除。

1. 确认目标 `provider_id` 和环境；
2. 在厂商控制台创建新 Key，不要立即撤销旧 Key；
3. 更新 `providers/<provider_id>/secrets.json` 或对应 Secret Manager；
4. 通过该 Provider 的 `probe.py` 执行最小真实探测；只有获得费用授权才能调用厂商；
5. 确认新 Key 生效后撤销旧 Key；
6. 检查日志、错误对象和 `provider_raw` 中没有泄漏。

智能体不得在未获得明确授权时创建付费厂商资源、撤销仍在使用的 Key 或修改生产租户权限。
修改任何 `.env` 环境变量后必须重启，不能把环境变量误报为已热加载。

修改现有 JSON 时必须保留不在任务范围内的 Token 和元数据，不能用样例文件覆盖真实文件。
读取密钥只用于目标操作，不得在工具输出、差异摘要或最终回答中打印完整值。若必须确认具体
密钥，由用户在本地自行核对；智能体只报告脱敏 `key_id` 和操作结果。

## 配方 D1：新增、替换、删除一把上游密钥

只操作已确认项目的 `providers/<provider_id>/secrets.json`。即使只有一把也使用 api_keys 数组。
真实密钥由用户在本地安全输入，或从已授权的现有文件取得；不要放进 shell 参数和会被回显的命令。

| 操作 | 修改位置 | 必须保留 |
| --- | --- | --- |
| 新增备用密钥 | api_keys 末尾追加一个对象 | 原有项、key_id、顺序、enabled 状态 |
| 替换指定密钥 | 找到目标 key_id，只替换该项 api_key | 目标 key_id 与 enabled，所有其他密钥 |
| 暂停指定密钥 | 目标项 enabled=false | 至少一项 enabled=true；否则改用厂商启停 |
| 删除指定密钥 | 删除已经确认的目标项 | 至少一项启用密钥；未授权不撤销厂商端凭据 |

新增示例只表示**一个待追加项**，不是整份文件：

```json
{"key_id":"backup-2","api_key":"EXAMPLE_ONLY_REPLACE_LOCALLY","enabled":true}
```

保存前检查：api_keys 是数组，key_id 非空且唯一，api_key 是非空字符串，enabled 是 JSON 布尔值
`true/false` 而不是字符串 `"true"`。不得删除其他自定义字段，不得顺便迁移到 `.env`。
旧单密钥格式迁移时先保留原值建立 primary，再合并新项；验证无误后移除旧顶层 api_key/api_key_id，
不要让旧值继续覆盖新池。不要为迁移打印原文件。

保存方式：在目标文件同目录生成 UTF-8（无 BOM）临时文件，序列化并校验 JSON 后原子替换；
使用受限权限，不写到公共临时目录，不产生会被提交的 `.bak` 密钥副本。
若配置仍被其他进程或用户编辑，检测到变化就重新合并，不能覆盖并发修改。

JSON 可读不等于新密钥有效。修改可热更新，但要确认下一次配置加载未报错、管理端看得到目标 key_id。
“可用/healthy”可能只是初始或冷却后的路由状态，不等于做过新 Key 的真实验证。
池探测成功也不一定用了刚加入的 Key；要验证具体 Key，须在用户授权下测试明确绑定该 Key 的 Client，
不能为验证而擅自关闭所有其他密钥。真实调用未授权就报告“已保存，尚未验证上游鉴权”。

## 配方 D2：修改网关调用 Token 或模型白名单

完整离线结构见 `template/examples/gateway-keys.json.example`。注意实际形状：

```text
api/keys.json
  keys（对象，不是数组）
    Token 原文作为键
      key_id / tenant_id / subject_id / scopes / allowed_models
```

这与 Provider 的 `api_keys` 数组完全不同，不能互相复制。

1. 从用户指定的 key_id 找到对应 Token 条目，不按网页显示的名称猜 Token。
2. 只改模型限制时，修改该条目的 allowed_models，不修改 Token、tenant_id、subject_id 或 scopes。
3. `allowed_models=null` 允许 scope 范围内全部已启用模型；`[]` 全部禁止；数组使用**完整网关模型名**。
   白名单不是能力开关，不会创建模型，也不能越过全局禁用策略。
4. 更换 Token 时保留这组身份与权限信息。需要新旧并行时用不同 key_id，不能偷偷扩大到 owner。
5. 先按授权验证新 Token，再按用户明确要求删除旧条目；不要直接覆盖整个 keys 对象。
6. 用调用方自己的 Token 查询模型目录验证可见范围；不拿 owner 的目录当成普通密钥的结果。

对应 scope：LLM 用 `model:invoke`，Embedding 用 `embedding:invoke`，Rerank 用 `rerank:invoke`。
只授予用户要求的权限；不要为了查询不到模型而授予 owner。

## 配方 D3：只改连接地址或请求头

读取目标 `config.json`，只修改 base_url、timeout_seconds 或该 Provider 支持的非敏感
default_headers，保留其余配置。不删除 secrets.json，不重新初始化密钥池。
Authorization 应由 Client 使用当前注入的 api_key 构造；额外敏感头需要先核对本包实现，
不能直接写入非敏感 config.json。保存后下一请求热加载，加载失败时旧有效配置可能仍在服务，
因此必须确认加载状态，不能只看进程仍能响应。

## 失败时不要这样“修”

- `CSRF` 或管理鉴权失败：检查会话、同源与 CSRF 流程，不关闭鉴权，不将 Web Token 改成调用密钥。
- 模型看不到：检查 scope、白名单、注册和启停，不直接设为全部允许。
- 新 Key 不可用：保留旧 Key，报告脱敏错误；没有授权不撤销旧 Key 或反复付费试错。
- 修改后未生效：检查 JSON、UTF-8 编码和加载错误，不清空配置、不删除运行数据库。
