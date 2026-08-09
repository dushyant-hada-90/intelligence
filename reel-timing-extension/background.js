/**
 * Extension-origin fetch to the local FastAPI server.
 * MAIN-world page fetch from https://instagram.com → http://127.0.0.1 is blocked
 * by Chrome (private network / mixed content). host_permissions apply here.
 */
chrome.runtime.onMessage.addListener(function (msg, _sender, sendResponse) {
  if (!msg || msg.type !== "REEL_API") {
    return;
  }

  var base = String(msg.baseUrl || "").replace(/\/+$/, "");
  var path = msg.path || "/reels";
  var url = base + path;

  fetch(url, {
    method: msg.method || "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify(msg.body == null ? [] : msg.body),
  })
    .then(function (res) {
      if (!res.ok) {
        throw new Error("HTTP " + res.status);
      }
      return res.json();
    })
    .then(function (data) {
      sendResponse({ ok: true, data: data });
    })
    .catch(function (err) {
      sendResponse({
        ok: false,
        error: (err && err.message) || String(err),
      });
    });

  return true; // keep channel open for async sendResponse
});
