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

复制模板后不得仅做字符串替换。尤其要重新实现并验证厂商的媒体来源转换、错误正文解析、
任务端点和能力声明；“OpenAI-compatible”不等于自动支持图片、音频、工具、推理或流式。完整
多模态实现必须使用 `RequestContext.assets` 读取输入或登记输出，并逐项声明
`extensions.operations`，不能向公开响应泄露本地路径。

每个 LLM 模型还必须显式填写 `reasoning`。模板默认不支持推理；一旦确认支持，必须面向
kemo-agent 暴露 `minimal|low|medium|high|max` 五个逻辑档位并逐项映射。厂商档位较少或
只有开关时允许折叠映射，但必须在能力扩展中公开，不能把五个名称盲目原样透传。
