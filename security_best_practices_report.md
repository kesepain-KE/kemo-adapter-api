# Kemo Gateway Web 管理端安全审查报告

审查日期：2026-07-29  
范围：FastAPI/Uvicorn 管理后端、React/Vite 管理前端、启动配置与密钥管理接口。

## 执行摘要

审查确认旧实现会把 Web Token 放入 URL、把管理会话放入 `sessionStorage`，并由密钥列表接口把完整网关调用密钥返回浏览器。以上高风险问题已经修复。当前浏览器只持有不可用于登录的 CSRF Token；管理会话改为服务端随机会话和 HttpOnly Cookie；长期网关密钥及 Provider 请求头值不再回传浏览器。

公网发布仍依赖部署方正确提供 HTTPS、设置外部 `https://` Base URL，并配置 Host 白名单。浏览器操作者在 DevTools 中始终可以看到自己刚提交的登录表单正文，也能看到当前站点的短期不透明 Cookie；Web 应用无法对掌控浏览器的人隐藏这些数据。修复目标是确保这些内容不包含长期网关/Provider 密钥，且短期 Cookie 不能被页面 JavaScript 读取。

## High

### KEMO-SEC-001：长期 Web Token 曾进入 URL（已修复）

- Rule ID：FASTAPI-AUTH-002
- Severity：High
- Location：`api/server.py:237`、`start_web.py:199`、`web/frontend/src/main.tsx:9`
- Evidence：根路由现在无条件 303 到 `/admin`；启动器只打开无查询串 URL；前端启动时清除历史查询串/片段。访问日志仍保留旧参数脱敏兜底。
- Impact：旧设计可能通过地址栏、历史记录、反向代理日志、截图和 Referer 泄漏长期 Token。
- Fix：只允许在 HTTPS 登录表单 JSON 正文中提交 `WEB_TOKEN`。
- Mitigation：公网反向代理不要记录请求正文，并对旧版 `token` 查询参数做日志脱敏。
- False positive notes：即使前端随后 `replaceState`，请求已经经过浏览器和代理，因此旧设计仍构成泄漏。

### KEMO-SEC-002：管理会话曾暴露给页面 JavaScript（已修复）

- Rule ID：REACT-AUTH-001、FASTAPI-SESS-001、FASTAPI-SESS-002
- Severity：High
- Location：`web/backend/router.py:85-99`、`api/middleware.py:112-125`、`web/frontend/src/AdminContext.tsx:31-39`
- Evidence：服务端设置 `HttpOnly`、`SameSite=Strict`、限定 `/admin` 路径的随机 Cookie；前端不再使用 `sessionStorage` 或 Authorization Bearer 保存会话。
- Impact：旧设计下任意同源 XSS 都能直接读取并外传两小时 owner 会话。
- Fix：Cookie 中仅保存不透明随机会话 ID，敏感会话状态留在服务端内存。
- Mitigation：公网设置 Secure Cookie、CSP，并保持前端无危险 HTML 注入点。
- False positive notes：HttpOnly 不会阻止本机浏览器操作者从 DevTools 查看 Cookie；它阻止的是页面 JavaScript/XSS 直接读取。

### KEMO-SEC-003：密钥列表曾返回完整网关调用密钥（已修复）

- Rule ID：FASTAPI-RESP-001、REACT-CONFIG-001
- Severity：High
- Location：`web/backend/router.py:315-415`、`web/frontend/src/adminApi.ts:218-246`、`web/frontend/src/pages/Keys.tsx:120-129`
- Evidence：接口字段改为 `masked_token`，只保留少量尾部识别字符；完整 `token` 字段和前端查看/复制按钮已删除。
- Impact：旧实现中，任何已登录 owner 打开 Network/React DevTools 都能获得并复制全部调用密钥；浏览器扩展或 XSS 也能批量窃取。
- Fix：列表只返回元数据、安全掩码和统计；完整密钥应在创建时一次性保存，遗失后轮换。
- Mitigation：继续使用 `Cache-Control: no-store`，并避免未来新增“恢复明文密钥”接口。
- False positive notes：CSS 遮罩不是安全控制；只要响应含完整值，DevTools 就一定可见。

### KEMO-SEC-004：Provider 自定义请求头可能回显密钥（已修复）

- Rule ID：FASTAPI-RESP-001、REACT-CONFIG-001
- Severity：High
- Location：`web/backend/service.py:112-130`、`web/backend/service.py:148-178`、`web/frontend/src/pages/Settings.tsx:286-290`
- Evidence：所有 `default_headers`/`headers` 只返回名称，值统一为空；保存空值时后端保留现有服务器值。敏感键匹配同时支持连字符等标点。
- Impact：如 `X-API-Key`、`Authorization` 等头值回传浏览器，会泄漏 Provider 长期凭据。
- Fix：请求头值和 Provider API Key 均采用只写不回显设计。
- Mitigation：新增 Provider 时仍应把真正凭据放在 `secrets.json`，不要混入普通 diagnostics/config 响应。
- False positive notes：管理员首次写入新值时，该值必然存在于其浏览器请求正文；无法向掌控该浏览器的人隐藏，但保存后不会再次下发。

### KEMO-SEC-005：Cookie 写操作缺少 CSRF 防护（已修复）

- Rule ID：FASTAPI-CSRF-001、REACT-CSRF-001
- Severity：High
- Location：`web/backend/router.py:103-140`、`web/backend/router.py:431,596,675,697,715,738`、`web/frontend/src/adminApi.ts:277-289`
- Evidence：所有管理写接口要求每会话随机 `X-CSRF-Token`，并校验 Origin/Referer 与 Fetch Metadata；Bearer 管理 API 客户端不受 Cookie CSRF 规则影响。
- Impact：缺少防护时，恶意站点可能借已登录管理员的 Cookie 修改配置、探测模型或触发重启。
- Fix：服务端会话绑定 CSRF Token，前端只在非安全方法中发送该 Header。
- Mitigation：SameSite=Strict、CSP、无 CORS 共同提供纵深防御。
- False positive notes：纯 Authorization Header 客户端不由浏览器自动附加凭据，因此不需要 Cookie CSRF Token。

### KEMO-SEC-006：非回环监听可在空认证下暴露 owner 控制台（产品接受风险）

- Rule ID：FASTAPI-AUTH-001、REACT-AUTHZ-001
- Severity：High
- Location：`api/server.py`、`start_web.py`、`.env.example`
- Evidence：按局域网免配置部署要求，Web Token、用户名和密码全部为空时，回环、局域网地址及 `0.0.0.0` 监听均允许启动，并向可访问管理端的客户端授予 owner 权限。
- Impact：如果无鉴权实例被直接暴露到公网，任何访问者都能够读取管理数据、修改配置或触发重启。
- Decision：产品明确选择“凭据全空即免登录”作为可信局域网部署模式，不再使用额外的不安全开关。
- Mitigation：公网部署必须同时配置 Web Token 与用户名/密码，使用 HTTPS、Secure Cookie、Host 白名单、防火墙或反向代理访问控制。
- False positive notes：这不是误报；属于需要部署者根据网络边界主动控制的已知风险。

## Medium

### KEMO-SEC-007：登录端点缺少防爆破限制（已修复）

- Rule ID：FASTAPI-AUTH-001（认证纵深防御）
- Severity：Medium
- Location：`web/backend/auth_service.py:71-105`、`web/backend/router.py:64-82`
- Evidence：每阶段、每客户端在 15 分钟内最多允许 8 次失败；超限返回 429 与 `Retry-After`。跟踪表和活跃会话表都有容量上限。
- Impact：公网登录端点可能被持续猜测 Token 或密码并造成内存压力。
- Fix：加入固定窗口限流和成功后清理。
- Mitigation：反向代理/WAF 再增加 IP 级速率限制；代理必须正确、受信地传递客户端地址。
- False positive notes：进程重启会清空内存限流状态；边缘限流仍有价值。

### KEMO-SEC-008：缺少浏览器安全头且 OpenAPI 默认公开（已修复）

- Rule ID：FASTAPI-OPENAPI-001、REACT-HEADERS-001、REACT-CSP-001
- Severity：Medium
- Location：`api/server.py:139-180`、`core/config.py:37-43`、`.env.example:71`
- Evidence：默认关闭 `/docs`、`/redoc`、`/openapi.json`；响应加入 CSP、nosniff、DENY frame、no-referrer、Permissions Policy、COOP/CORP；管理文档和 API 禁止缓存。
- Impact：缺少这些控制会放大 XSS、点击劫持、接口枚举和敏感页面缓存风险。
- Fix：后端统一设置安全头，仅在受信开发环境显式开启 API 文档。
- Mitigation：反向代理可重复设置同等或更严格的策略，但不得放宽 CSP。
- False positive notes：动态图表使用 React inline style，因此 CSP 只对 `style-src-attr` 允许 inline；脚本仍严格限制为同源。

### KEMO-SEC-009：公网 TLS 与 Host 白名单属于部署责任（开放的操作项）

- Rule ID：FASTAPI-SESS-001、FASTAPI-HOST-001
- Severity：Medium
- Location：`.env.example:37-48`、`api/server.py:150-154`
- Evidence：代码支持 `WEB_COOKIE_SECURE=auto/true` 和 `WEB_ALLOWED_HOSTS`；没有仓库内反向代理证书/边缘配置可供验证。
- Impact：若公网仍使用 HTTP，登录 Token、密码和会话 Cookie 可被链路窃听；未限制 Host 会增加 Host Header 攻击面。
- Fix：公网反向代理终止 HTTPS；设置 `GATEWAY_BASE_URL=https://实际域名`、`WEB_COOKIE_SECURE=auto`、`WEB_ALLOWED_HOSTS=实际域名`。
- Mitigation：只允许代理访问后端监听端口，关闭直接公网访问；运行时检查实际响应的 Set-Cookie 和安全头。
- False positive notes：TLS 常由 Nginx、Caddy、Cloudflare 等仓库外设施提供，因此这里不能仅凭应用代码判定缺失。

## Informational

### KEMO-SEC-010：DevTools 可见性边界

- Rule ID：浏览器信任边界说明
- Severity：Informational
- Location：`web/frontend/src/App.tsx:36-104`、`web/backend/router.py:181-255`
- Evidence：登录输入通过浏览器表单提交；成功后响应不再包含服务端会话 ID，只含失窃后不能单独登录的 CSRF Token。Cookie 是短期随机值，不是原始 Web Token、密码、网关调用密钥或 Provider 密钥。
- Impact：拥有浏览器/操作系统控制权的人仍能查看其输入、网络请求和 Cookie，并可在会话有效期内冒用当前浏览器权限。
- Fix：Web 技术无法对终端操作者隐藏终端自己发送的数据；安全边界必须依靠终端访问控制、HTTPS、短期会话和服务端不下发长期密钥。
- Mitigation：管理端只在可信设备使用；不要共享浏览器配置文件；操作完成后退出；高风险公网场景可在反向代理增加 VPN、mTLS 或身份感知访问控制。
- False positive notes：这是浏览器设计属性，不是可通过混淆 JavaScript、禁用右键或隐藏 DevTools 修复的漏洞。

## 验证结果

- `python -m pytest -q`：403 passed。
- `pnpm run build`：TypeScript 与 Vite 生产构建成功。
- `pnpm audit --prod`：No known vulnerabilities found。
- `git diff --check`：无空白错误。
