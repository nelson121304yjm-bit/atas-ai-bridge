"use strict";
// Runs in the head before any body content can render.
(() => {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const pending = [];
  const seen = new Set();
  const browserToken = fragment.get("token");
  if (browserToken) pending.push({token: browserToken, requestId: null});
  history.replaceState(null, "", location.pathname + location.search);
  // The installed ATAS macOS backend exposes this WebView2-compatible channel.
  // There is deliberately no generic window-message/RPC listener.
  const channel = window.chrome?.webview;
  window.reviewBootstrap = {
    take: () => pending.shift() || null,
    hasToken: () => pending.length > 0,
    complete(value, ok) {
      if (value?.requestId && channel?.postMessage)
        channel.postMessage({type: "atas-ai-review.session", version: 1, requestId: value.requestId, ok});
    },
  };
  channel?.addEventListener("message", event => {
    // Only a local top-level root document may accept a bootstrap. Native code
    // independently checks the exact current URL before sending credentials.
    if (window.top !== window || location.protocol !== "http:" || location.hostname !== "127.0.0.1" ||
        !/^(877[0-9]|8780)$/.test(location.port) || location.pathname !== "/" || location.search || location.hash)
      return;
    const value = event.data;
    if (!value || value.type !== "atas-ai-review.bootstrap" || value.version !== 1 ||
        typeof value.token !== "string" || !/^[A-Za-z0-9_-]{20,256}$/.test(value.token) ||
        typeof value.requestId !== "string" || !/^[a-f0-9]{32}$/.test(value.requestId) ||
        seen.has(value.token) || seen.size >= 32 || pending.length >= 4)
      return;
    seen.add(value.token);
    pending.push({token: value.token, requestId: value.requestId});
    window.dispatchEvent(new Event("review-bootstrap"));
  });
})();
