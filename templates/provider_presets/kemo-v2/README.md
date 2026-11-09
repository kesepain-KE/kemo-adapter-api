# Kemo Protocol 2.0

使用 `kemo-v2` 接入另一个 Kemo 2.0 网关。地址填写网关根地址，不附加 `/model`。填写上游下游密钥；内网地址显式开启允许私网。媒体传递要求密钥具有 `asset:read`、`asset:write` 权限。

模型登记直接导入上游能力声明，不执行付费生成；“测试模型”执行真实请求。具体配置和联邦限制见 [智能体手册](../../../agent/_doc/kemo-upstreams.md)。
