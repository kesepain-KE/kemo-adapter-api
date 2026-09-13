"""Read-only, allow-listed view of the settings loaded by this process.

Never serialize Settings, os.environ, .env, or private Provider dictionaries.
Only explicitly identified credentials receive five/three edge previews.
Arbitrary private dictionaries, headers and short values remain hidden.
"""

from core.config import Settings
from core.live_config import LiveConfigSnapshot
from core.provider_keys import normalize_provider_keys
from core.secret_preview import mask_secret


GROUPS = (
    ("网络与服务", (
        ("HOST", "host", "监听地址"),
        ("PORT", "port", "监听端口"),
        ("GATEWAY_BASE_URL", "base_url", "对外 Base URL"),
        ("API_DOCS_ENABLED", "api_docs_enabled", "公开 API 文档"),
        ("STATISTICS_TIMEZONE", "statistics_timezone", "统计时区"),
    )),
    ("Web 认证与访问", (
        ("WEB_COOKIE_SECURE", "web_cookie_secure", "Cookie Secure 策略"),
        ("WEB_ALLOWED_HOSTS", "web_allowed_hosts", "允许访问的主机"),
    )),
    ("执行与流式传输", (
        ("MAX_CONCURRENT_EXECUTIONS", "max_concurrent_executions", "最大并发执行数"),
        ("MODEL_EXECUTION_TIMEOUT_SECONDS", "model_execution_timeout_seconds", "执行超时（秒）"),
        ("SSE_HEARTBEAT_SECONDS", "sse_heartbeat_seconds", "SSE 心跳间隔（秒）"),
        ("EXECUTION_RETENTION_HOURS", "execution_retention_hours", "执行记录保留（小时）"),
        ("MAX_SSE_EVENTS_PER_RESPONSE", "max_sse_events_per_response", "单次响应事件上限"),
    )),
    ("请求与媒体资源", (
        ("REQUEST_JSON_MAX_BYTES", "request_json_max_bytes", "请求 JSON 上限（字节）"),
        ("DEFAULT_ASSET_TTL_HOURS", "asset_retention_hours", "媒体资源保留（小时）"),
        ("ASSET_IMAGE_MAX_BYTES", "asset_image_max_bytes", "图片上限（字节）"),
        ("ASSET_AUDIO_MAX_BYTES", "asset_audio_max_bytes", "音频上限（字节）"),
        ("ASSET_VIDEO_MAX_BYTES", "asset_video_max_bytes", "视频上限（字节）"),
        ("ASSET_FILE_MAX_BYTES", "asset_file_max_bytes", "文件上限（字节）"),
    )),
)

SECRET_FIELDS = (
    ("WEB_USERNAME", "管理用户名"),
    ("WEB_PASSWORD", "管理密码"),
    ("WEB_TOKEN", "Web Token"),
    ("STATUS_TOKEN", "状态接口 Token"),
    ("GATEWAY_API_KEY", "网关调用密钥（含批量与文件配置）"),
    ("PROVIDER_SETTINGS", "Provider 私有配置与密钥"),
)


def gateway_config_view(
    settings: Settings, snapshot: LiveConfigSnapshot | None = None,
) -> dict[str, object]:
    groups = []
    for title, fields in GROUPS:
        items = []
        for name, attribute, label in fields:
            raw = getattr(settings, attribute)
            if raw is None:
                value = "auto"
            elif isinstance(raw, bool):
                value = "true" if raw else "false"
            elif isinstance(raw, tuple):
                value = ", ".join(raw)
            else:
                value = str(raw)
            # This field is an address hint, not an arbitrary URL inspector.
            # Hide the entire value if it contains credentials or URL parameters.
            if attribute == "base_url" and any(char in value for char in "@?#"):
                value = "***"
            items.append({"name": name, "label": label, "value": value, "sensitive": False})
        groups.append({"title": title, "items": items})
    keys = dict(settings.api_keys)
    provider_settings = dict(settings.provider_settings)
    if snapshot is not None:
        keys.update(snapshot.api_keys)
        for provider_id, config in snapshot.provider_settings.items():
            provider_settings[provider_id] = {**provider_settings.get(provider_id, {}), **config}
    provider_masks = []
    for config in provider_settings.values():
        if not isinstance(config, dict):
            continue
        try:
            provider_masks.extend(mask_secret(secret) for _, secret, _ in normalize_provider_keys(config))
        except (ValueError, TypeError):
            # Malformed private configuration is not a previewable string.
            provider_masks.append("***")
    masks = {
        "WEB_USERNAME": mask_secret(settings.web_username),
        "WEB_PASSWORD": mask_secret(settings.web_password),
        "WEB_TOKEN": mask_secret(settings.web_token),
        "STATUS_TOKEN": mask_secret(settings.status_token),
        "GATEWAY_API_KEY": "\n".join(mask_secret(token) for token in keys) or "***",
        "PROVIDER_SETTINGS": "\n".join(provider_masks) or "***",
    }
    groups.append({"title": "敏感配置", "items": [
        {"name": name, "label": label, "value": masks[name], "sensitive": True}
        for name, label in SECRET_FIELDS
    ]})
    return {"read_only": True, "source": "running_process", "groups": groups}
