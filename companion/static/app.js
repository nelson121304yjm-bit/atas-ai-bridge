"use strict";

const fragment = new URLSearchParams(location.hash.slice(1));
const launchToken = fragment.get("token");
history.replaceState(null, "", location.pathname + location.search);

async function connect() {
  const status = document.getElementById("service");
  try {
    const response = launchToken
      ? await fetch("/api/bootstrap", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({token: launchToken}),
        })
      : await fetch("/api/session");
    if (!response.ok) throw new Error("session");
    const session = await response.json();
    const service = await fetch("/api/status");
    if (!service.ok) throw new Error("service");
    const state = await service.json();
    status.textContent = state.configured ? "本地服务已连接" : "本地服务已连接 · 尚未配置";
    window.reviewSession = {csrf: session.csrf};
  } catch {
    status.textContent = "无法连接。请重新从 ATAS 图表或启动器打开工作台。";
  }
}

document.addEventListener("DOMContentLoaded", connect);
