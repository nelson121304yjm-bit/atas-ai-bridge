"use strict";
// Runs in the head before any body content can render.
const fragment = new URLSearchParams(location.hash.slice(1));
window.reviewBootstrap = {token: fragment.get("token")};
history.replaceState(null, "", location.pathname + location.search);
