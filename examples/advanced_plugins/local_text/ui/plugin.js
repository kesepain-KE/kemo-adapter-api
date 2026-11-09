let port, nonce, counter = 0;
const pending = new Map();
async function refresh() {
  const [config, models] = await Promise.all([call("plugin.config.read"), call("plugin.model.list")]);
  document.querySelector("#status").textContent = models.models.length ? "运行中" : "插件未启用或没有可用模型";
  document.querySelector("#models").textContent = String(models.models.length);
  document.querySelector("#mode").value = config.values.mode || "uppercase";
  document.querySelector("#prefix").value = config.values.prefix || "";
}
function call(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++counter;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error("宿主响应超时")); }, 20000);
    pending.set(id, { resolve, reject, timer });
    port.postMessage({ version: 1, nonce, id, method, params });
  });
}
window.addEventListener("message", async event => {
  if (event.source !== parent || port || event.data?.type !== "kemo-plugin-init" || !event.ports[0]) return;
  port = event.ports[0]; nonce = event.data.nonce;
  port.onmessage = event => {
    const value = event.data, operation = pending.get(value.id);
    if (value.type === "plugin-context-changed" && value.nonce === nonce) { refresh().catch(error => { document.querySelector("#status").textContent = error.message; }); return; }
    if (!operation || value.nonce !== nonce) return;
    pending.delete(value.id); clearTimeout(operation.timer);
    if (value.error) operation.reject(new Error(value.error)); else operation.resolve(value.result);
  };
  try {
    await refresh();
    await call("plugin.ui.resize", { height: document.querySelector("main").getBoundingClientRect().height });
  } catch (error) { document.querySelector("#status").textContent = error.message; }
});
new ResizeObserver(() => {
  if (port) call("plugin.ui.resize", { height: document.querySelector("main").getBoundingClientRect().height })
    .catch(error => { document.querySelector("#status").textContent = error.message; });
}).observe(document.querySelector("main"));
document.querySelector("#save").addEventListener("click", async () => {
  try {
    await call("plugin.config.update", { mode: document.querySelector("#mode").value, prefix: document.querySelector("#prefix").value });
    document.querySelector("#feedback").textContent = "配置已保存，新请求使用新配置。";
  } catch (error) { document.querySelector("#feedback").textContent = error.message; }
});
