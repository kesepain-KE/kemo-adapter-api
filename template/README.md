# 模板目录

本目录存放 Kemo 网关可复制的创建模板。模板只描述核心契约和厂商边界，
厂商协议、计量、错误和流解析必须留在复制后的 Provider 目录内。

## 模板清单

| 目录 | 用途 | 参考实现 |
|------|------|---------|
| `provider/` | 创建新的厂商 Provider 包（含自有可达性探测器） | 本目录即权威骨架 |

## 使用方式

复制 `template/provider/` 到 `providers/<provider_id>/`，删除缓存，按需去掉 `.example` 后缀，
再根据厂商真实协议和脱敏 Fixture 完成实现。不要在 `providers/` 下维护第二份模板，也不要用
模板覆盖现有 Provider。完整流程见 `ADD_DIY/provider-package.md` 和
`ADD_DIY/verification.md`。
