# 模板目录

本目录存放 Kemo 网关可复制的创建模板。模板只描述核心契约和厂商边界，
厂商协议、计量、错误和流解析必须留在复制后的 Provider 目录内。

## 模板清单

| 目录 | 用途 | 参考实现 |
|------|------|---------|
| `provider/` | 创建新的 LLM 厂商 Provider 包 | `providers/deepseek/`（本地测试厂商） |

## 使用方式

复制 `template/provider/` 到 `providers/<provider_id>/`，去掉配置文件的
`.example` 后缀，再按厂商真实协议完成 TODO。不要在 `providers/` 下维护第二份模板。
