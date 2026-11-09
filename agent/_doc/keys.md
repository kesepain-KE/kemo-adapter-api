# 下游调用密钥

[返回操作入口](README.md) · 先完成 [连接与认证](connection.md)

这些 Token 供智能体调用模型，和提供商的上游 API Key、Web 管理凭据不同。修改、复制和删除需要 owner，revision 使用快照的 **`key_revision`**。

## 创建

`POST /admin/api/keys`：

```json
{
  "expected_revision": "<编辑前的 key_revision>",
  "value": {
    "name": "我的智能体",
    "allowed_models": ["my-assistant"],
    "allowed_ops": ["llm"],
    "rpm": 30,
    "tpm": 0,
    "budget": null,
    "currency": "USD",
    "enabled": true
  }
}
```

RPM 是用户选择的策略，示例 30 不是推荐额度。未指定 `token` 时网关生成随机 Token；确有导入需求时创建请求的 `value.token` 可传用户指定的安全值，须满足随机性及长度校验。

成功返回 `id`、`token`、`key_revision`。将原文直接写入用户指定的受保护客户端配置或密钥存储，不在操作报告里展示。写入成功后的刷新失败不代表创建失败，不能因此重复创建。

## 策略含义

| 字段 | 含义 |
| --- | --- |
| `allowed_models: null` | 允许全部模型，包括以后新增的模型 |
| `allowed_models: []` | 禁止全部模型 |
| `allowed_models: ["my-assistant"]` | 仅允许列出的公开模型 |
| `allowed_ops` | `llm`、`embedding`、`rerank` 的子集，空数组禁止模型任务 |
| `rpm`、`tpm` | 0 表示不限，正整数表示限额 |
| `budget` | `null` 不设预算；数值按配置币种控制累计调用费用，0 不是无限额 |
| `currency` | 预算支持 `USD`、`CNY`、`EUR`、`JPY`、`GBP` |
| `enabled` | false 拒绝该 Token 的新调用 |
| `name`、`note` | 用于区分调用者和用途 |

启用 TPM 需要模型有明确 Token 硬上限；预算还需要所有可选 Deployment 有完整同币种价格。不满足时调用会被拒绝，不用放大白名单、编造上限或写 0 价格来绕过。网关不把不同币种自动换算。

## 编辑、停用与恢复

`PATCH /admin/api/keys/{key_id}`，带 `key_revision`，`value` 仅包含要修改的策略。例如将智能体限制到两个已确认的模型：

```json
{"allowed_models": ["my-assistant", "my-backup"], "allowed_ops": ["llm"]}
```

服务端按字段合并策略；数组整体替换，未提到的字段保留。停用为 `{"enabled": false}`，恢复为 `true`。这些变更无需重启，使用调用方 Token 重新读取模型目录验证实际可见性。

## 按需复制原文

仅在用户任务需要使用或迁移该 Token 时调用 `POST /admin/api/keys/{key_id}/reveal`，正文 `{expected_revision: <key_revision>, value: {}}`。响应禁止缓存，并记录审计。普通快照的 `preview` 或 `prefix` 不是完整 Token。

`can_copy: false` 表示历史密钥只保留哈希，无法还原；现有 Token 仍可使用。需要新原文时创建替代密钥并更新客户端，不尝试从哈希或掩码恢复。

## 轮换与删除

下游 Token 的编辑只修改策略，不直接替换原文。轮换步骤：

1. 读取旧密钥策略，创建具有用户所需策略的新密钥。
2. 把新 Token 写入用户指定客户端，使用该 Token 读取目录与能力确认权限。
3. 根据用户的轮换范围停用旧密钥，确认后删除；仍被其他客户端使用的旧密钥需按用户要求保留。

`DELETE /admin/api/keys/{key_id}` 带 `key_revision` 和空 `value`。删除后拒绝新调用，历史日志和费用保留。每一步用重新读取后的 revision，不能把目录 revision 用于密钥操作。
