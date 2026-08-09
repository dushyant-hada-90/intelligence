/**
 * ISOLATED-world bridge: MAIN content-script ↔ background fetch.
 * DOM CustomEvents are shared across worlds; chrome.runtime is not available in MAIN.
 */
(function () {
  "use strict";

  // Only top frame — avoid duplicate handlers in IG iframes.
  if (window !== window.top) {
    return;
  }

  window.addEventListener("reel-timing-api-request", function (ev) {
    try {
      var detail = ev && ev.detail;
      if (!detail || !detail.reqId) {
        return;
      }
      if (!chrome || !chrome.runtime || !chrome.runtime.sendMessage) {
        window.dispatchEvent(
          new CustomEvent("reel-timing-api-response", {
            detail: {
              reqId: detail.reqId,
              ok: false,
              error: "chrome.runtime unavailable",
            },
          })
        );
        return;
      }

      chrome.runtime.sendMessage(
        {
          type: "REEL_API",
          reqId: detail.reqId,
          baseUrl: detail.baseUrl,
          path: detail.path,
          method: detail.method || "POST",
          body: detail.body,
        },
        function (resp) {
          var err =
            chrome.runtime.lastError && chrome.runtime.lastError.message;
          window.dispatchEvent(
            new CustomEvent("reel-timing-api-response", {
              detail: {
                reqId: detail.reqId,
                ok: !err && resp && resp.ok,
                data: resp && resp.data,
                error: err || (resp && resp.error) || "unknown_bridge_error",
              },
            })
          );
        }
      );
    } catch (e) {
      try {
        window.dispatchEvent(
          new CustomEvent("reel-timing-api-response", {
            detail: {
              reqId: ev && ev.detail && ev.detail.reqId,
              ok: false,
              error: (e && e.message) || String(e),
            },
          })
        );
      } catch (_ignore) {
        /* ignore */
      }
    }
  });
})();
