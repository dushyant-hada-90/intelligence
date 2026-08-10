/**
 * Instagram Reel Metadata Timing Logger
 * Passive observer: intercept GraphQL reel batches + log viewport entry deltas.
 * Runs in MAIN world at document_start so fetch/XHR hooks see page traffic.
 */
(function () {
  "use strict";

  try {
    if (window.__REEL_TIMING_INSTALLED__) {
      return;
    }
    window.__REEL_TIMING_INSTALLED__ = true;

    var PREFIX = "[REEL-TIMING]";
    var CLIPS_EDGES_KEY = "xdt_api__v1__clips__home__connection_v2";

    /**
     * Slim metadata only (never full GraphQL media).
     * Ingest fields (music/likes/…) are cached here until the bot watches, then POSTed + released.
     * @type {Map<string, object>}
     */
    var reelMetadataMap = new Map();
    /** @type {string[]} global order for username FIFO correlation (compacted by prune) */
    var orderedReelIds = [];
    /** Soft caps so long sessions do not retain unbounded prefetch / done state. */
    var MAX_REEL_METADATA = 80;
    var MAX_DONE_TRACK = 300;
    var MAX_OBSERVED_CONTAINERS = 40;
    /** Ring of done reel ids (evicts oldest keys from autopilotDoneIds). */
    var doneIdRing = [];
    /** @type {Element[]} accepted reel containers in discovery order */
    var observedContainers = [];
    /** Stable index assigned at first accept (survives virtualized unmount). */
    var containerIndexMap = new WeakMap();
    var seenContainers = new WeakSet();
    var rejectedContainers = new WeakSet();
    var enteredElements = new WeakSet();
    /** Viewport fired before metadata was available — retry on next ingest. */
    var pendingElements = new WeakSet();
    /** @type {{ el: Element, domUsername: string|null, t: number, domPosition: number }[]} */
    var pendingViewports = [];
    var batchCounter = 0;
    var viewportHitCount = 0;
    var totalContainersAccepted = 0;
    var totalContainersRejected = 0;

    // --- Autopilot / engagement (server-driven) ---
    var AUTOPILOT_ENABLED = true;
    /** Local FastAPI backend (see ../backend). Change only if you bind a different host/port. */
    var API_BASE_URL = "http://127.0.0.1:7860";
    var DEFAULT_WATCH_DURATION_S = 5;
    /** Min watch time before commenting (humans read the reel first). */
    var MIN_COMMENT_WATCH_S = 15;
    var DECISION_WAIT_TIMEOUT_MS = 8000;
    var viewportMatchIndex = 0; // 1-based, same order as validation table
    var autopilotBusy = false;
    var autopilotFailStreak = 0;
    /** FIFO of matched reels — prevents ArrowDown double-advance from skipping jobs. */
    var autopilotQueue = [];
    /** @type {Record<string, true>} */
    var autopilotQueuedIds = Object.create(null);
    /** @type {Record<string, true>} */
    var autopilotDoneIds = Object.create(null);
    /** @type {(() => void)|null} */
    var autopilotReelWaiter = null;
    /** Last job handed to the loop (for focus/scroll targeting). */
    var currentAutopilotReel = null;

    /**
     * Server decisions: Map<code, { action, comment, duration, receivedAt }>
     * Filled by batch POST /reels — never one id at a time.
     */
    var reelDecisionMap = new Map();
    /** @type {Promise<void>|null} */
    var decisionFetchInFlight = null;
    /** Ids currently included in an in-flight batch request. */
    var decisionFetchIds = Object.create(null);

    /** Suppress metadata / viewport / bot chatter — only REEL_RESULT logs. */
    var QUIET_META_VIEWPORT_LOGS = true;
    var QUIET_BOT_LOGS = true;

    var LOG_COLORS = {
      META: "#5B9BD5",
      VIEWPORT: "#9B59B6",
      BOT: "#2ECC71",
      PLAN: "#E74C3C",
      WARN: "#E67E22",
      REPORT: "#F1C40F",
    };

    function logChannel(tag, color, silent) {
      return function () {
        if (silent) {
          return;
        }
        var args = Array.prototype.slice.call(arguments);
        if (!args.length) {
          return;
        }
        var first = String(args[0]);
        var rest = args.slice(1);
        var styled =
          "%c" + PREFIX + " [" + tag + "] " + first;
        var style =
          "color:" + color + ";font-weight:600";
        console.log.apply(console, [styled, style].concat(rest));
      };
    }

    var logMeta = logChannel(
      "META",
      LOG_COLORS.META,
      QUIET_META_VIEWPORT_LOGS
    );
    var logViewport = logChannel(
      "VIEWPORT",
      LOG_COLORS.VIEWPORT,
      QUIET_META_VIEWPORT_LOGS
    );
    var logBot = logChannel("BOT", LOG_COLORS.BOT, QUIET_BOT_LOGS);
    var logPlan = logChannel("PLAN", LOG_COLORS.PLAN, QUIET_BOT_LOGS);
    var logReport = logChannel("REPORT", LOG_COLORS.REPORT, false);

    /** Viewport order (reel ids) for every-10 expected vs observed reports. */
    var viewportReelOrder = [];
    var behaviorReportCursor = 0;
    /** @type {Map<string, { expected?: object, observed?: object }>} */
    var reelRunLog = new Map();

    function getRunEntry(reelId) {
      var entry = reelRunLog.get(reelId);
      if (!entry) {
        entry = { reelId: reelId, expected: null, observed: {} };
        reelRunLog.set(reelId, entry);
      }
      if (!entry.observed) {
        entry.observed = {};
      }
      return entry;
    }

    function recordExpected(reelId, decision) {
      var entry = getRunEntry(reelId);
      entry.expected = {
        action: decision && decision.action ? decision.action : null,
        comment:
          decision && decision.comment != null && decision.comment !== ""
            ? String(decision.comment)
            : null,
        duration_s:
          decision && decision.duration != null
            ? Number(decision.duration)
            : null,
      };
    }

    function recordObserved(reelId, patch) {
      var entry = getRunEntry(reelId);
      for (var k in patch) {
        if (Object.prototype.hasOwnProperty.call(patch, k)) {
          entry.observed[k] = patch[k];
        }
      }
    }

    function expectedVsObservedRow(reelId, index) {
      var entry = reelRunLog.get(reelId) || { expected: null, observed: {} };
      var exp = entry.expected || {};
      var obs = entry.observed || {};
      var meta = reelMetadataMap.get(reelId) || {};

      var expectedAction = exp.action || "none";
      var expectedComment = exp.comment || "none";
      var expectedDuration =
        exp.duration_s != null && isFinite(exp.duration_s)
          ? exp.duration_s
          : "n/a";

      var observedLiked = !!(obs.liked != null ? obs.liked : meta.liked);
      var observedSaved = !!(obs.saved != null ? obs.saved : meta.saved);
      var observedCommented = !!(
        obs.commented != null ? obs.commented : meta.commented
      );

      var observedAction = "none";
      if (observedLiked) {
        observedAction = "like";
      } else if (observedSaved) {
        observedAction = "save";
      }
      // Both like+save shouldn't happen from one action field; note if both.
      if (observedLiked && observedSaved) {
        observedAction = "like+save";
      }

      var actionOk = false;
      if (expectedAction === "none") {
        actionOk = !observedLiked && !observedSaved;
      } else if (expectedAction === "like") {
        actionOk = observedLiked;
      } else if (expectedAction === "save") {
        actionOk = observedSaved;
      }

      var commentOk =
        expectedComment === "none" ? !observedCommented : observedCommented;

      var dwellOk = true;
      var dwellActual = obs.dwell_actual_s;
      if (
        typeof expectedDuration === "number" &&
        typeof dwellActual === "number"
      ) {
        dwellOk = Math.abs(dwellActual - expectedDuration) <= 0.5;
      }

      var skipHard =
        !!obs.skipped_engage &&
        obs.skip_reason &&
        obs.skip_reason !== "decision_timeout";
      var overall =
        actionOk && commentOk && dwellOk && !skipHard ? "MATCH" : "MISMATCH";

      return {
        index: index,
        reel_id: reelId,
        expected_action: expectedAction,
        observed_action: observedAction,
        action_ok: actionOk,
        expected_comment: expectedComment,
        observed_commented: observedCommented,
        comment_ok: commentOk,
        expected_duration_s: expectedDuration,
        observed_dwell_s:
          typeof dwellActual === "number"
            ? Number(dwellActual.toFixed(2))
            : "n/a",
        dwell_ok: dwellOk,
        skip_reason: obs.skip_reason || "",
        result: overall,
      };
    }

    function printBehaviorReport(windowNum, reelIds) {
      try {
        var rows = [];
        var matches = 0;
        for (var i = 0; i < reelIds.length; i++) {
          var globalIndex = (windowNum - 1) * 10 + i + 1;
          var row = expectedVsObservedRow(reelIds[i], globalIndex);
          rows.push(row);
          if (row.result === "MATCH") {
            matches += 1;
          }
        }
        logReport(
          "BEHAVIOR_REPORT window=" +
            windowNum +
            " reels=" +
            reelIds.length +
            " match=" +
            matches +
            "/" +
            reelIds.length +
            " (expected from server vs observed bot)"
        );
        if (typeof console.table === "function") {
          console.table(rows);
        } else {
          for (var r = 0; r < rows.length; r++) {
            logReport(JSON.stringify(rows[r]));
          }
        }
        var mismatches = rows.filter(function (x) {
          return x.result !== "MATCH";
        });
        if (mismatches.length) {
          logPlan(
            "BEHAVIOR_MISMATCHES window=" +
              windowNum +
              " count=" +
              mismatches.length +
              " reel_ids=" +
              mismatches
                .map(function (m) {
                  return m.reel_id;
                })
                .join(",")
          );
        }
      } catch (_err) {
        warn("WARN printBehaviorReport failed");
      }
    }

    function maybePrintBehaviorReport() {
      // Per-reel REEL_RESULT replaces the every-10 table while QUIET_BOT_LOGS is on.
      return;
    }

    /** Default log channel = metadata / general diagnostics. */
    function log() {
      logMeta.apply(null, arguments);
    }

    function warn() {
      if (QUIET_BOT_LOGS && QUIET_META_VIEWPORT_LOGS) {
        return;
      }
      var args = Array.prototype.slice.call(arguments);
      if (!args.length) {
        return;
      }
      var first = String(args[0]);
      var rest = args.slice(1);
      console.warn.apply(
        console,
        [
          "%c" + PREFIX + " [WARN] " + first,
          "color:" + LOG_COLORS.WARN + ";font-weight:600",
        ].concat(rest)
      );
    }

    function decisionHasComment(decision) {
      return !!(
        decision &&
        decision.comment != null &&
        String(decision.comment) !== ""
      );
    }

    /**
     * `duration` = watch/dwell time only (human: watch first, then engage).
     * Comment plans require at least MIN_COMMENT_WATCH_S of watch.
     */
    function normalizePlanDurationS(decision) {
      var d = decision ? Number(decision.duration) : NaN;
      if (!(d > 0) || !isFinite(d)) {
        d = DEFAULT_WATCH_DURATION_S;
      }
      if (decisionHasComment(decision) && d < MIN_COMMENT_WATCH_S) {
        d = MIN_COMMENT_WATCH_S;
      }
      if (decision) {
        decision.duration = d;
      }
      return d;
    }

    /** One console line: plan vs what the bot actually did. */
    function logReelResult(job, decision, observed) {
      try {
        var expAction =
          decision && decision.action ? String(decision.action) : "none";
        var expComment =
          decision && decision.comment != null && decision.comment !== ""
            ? String(decision.comment)
            : "none";
        var expDur =
          decision && decision.duration != null
            ? Number(decision.duration)
            : null;
        var obs = observed || {};
        var watchPlanned = obs.dwell_planned_s;
        var watchActual = obs.watch_s;
        // Timing success = watched for plan duration (engage is extra, like a human).
        var timingOk =
          typeof watchPlanned === "number" &&
          typeof watchActual === "number" &&
          Math.abs(watchActual - watchPlanned) <= 0.08;

        var actionOk = true;
        var actionObs = "none";
        if (expAction === "like") {
          actionOk = !!obs.liked;
          actionObs = obs.liked ? "like" : "missed";
        } else if (expAction === "save") {
          actionOk = !!obs.saved;
          actionObs = obs.saved ? "save" : "missed";
        } else {
          actionOk = !obs.liked && !obs.saved;
          if (obs.liked) actionObs = "like(unexpected)";
          else if (obs.saved) actionObs = "save(unexpected)";
        }

        var commentOk = true;
        var commentObs = "none";
        if (expComment !== "none") {
          commentOk = !!obs.commented;
          commentObs = obs.commented ? "posted" : "missed";
        } else {
          commentOk = !obs.commented;
          commentObs = obs.commented ? "posted(unexpected)" : "none";
        }

        var skip = obs.skip_reason || "";
        var overall =
          !skip && timingOk && actionOk && commentOk ? "OK" : "FAIL";

        var msg =
          "REEL_RESULT index=" +
          job.index +
          " id=" +
          job.reelId +
          " | PLAN action=" +
          expAction +
          " comment=" +
          (expComment === "none" ? "none" : JSON.stringify(expComment)) +
          " watch_s=" +
          (expDur != null ? expDur : "n/a") +
          " | DONE watch=" +
          (typeof watchActual === "number"
            ? watchActual.toFixed(3) + "s"
            : "n/a") +
          (timingOk ? "(ok)" : "(skew)") +
          " engage=" +
          (typeof obs.engage_s === "number"
            ? obs.engage_s.toFixed(2) + "s"
            : "n/a") +
          " action=" +
          actionObs +
          " comment=" +
          commentObs +
          (skip ? " skip=" + skip : "") +
          " | " +
          overall;

        var color =
          overall === "OK" ? LOG_COLORS.BOT : LOG_COLORS.PLAN;
        console.log(
          "%c" + PREFIX + " [REPORT] " + msg,
          "color:" + color + ";font-weight:600"
        );
      } catch (_err) {
        /* ignore */
      }
    }

    function getApiBaseUrl() {
      return String(API_BASE_URL || "").replace(/\/+$/, "");
    }

    /**
     * POST JSON via extension background (host_permissions).
     * Direct page fetch from HTTPS Instagram → HTTP localhost is blocked by Chrome.
     */
    function extensionApiPost(path, body, timeoutMs) {
      var reqId =
        "r" +
        String(performance.now()) +
        "_" +
        String(Math.floor(Math.random() * 1e9));
      var ms = timeoutMs || 15000;
      return new Promise(function (resolve, reject) {
        var settled = false;
        function cleanup() {
          window.removeEventListener(
            "reel-timing-api-response",
            onResponse
          );
        }
        function onResponse(ev) {
          try {
            var detail = ev && ev.detail;
            if (!detail || detail.reqId !== reqId) {
              return;
            }
            if (settled) {
              return;
            }
            settled = true;
            cleanup();
            if (detail.ok) {
              resolve(detail.data);
            } else {
              reject(new Error(detail.error || "extension_api_failed"));
            }
          } catch (err) {
            if (!settled) {
              settled = true;
              cleanup();
              reject(err);
            }
          }
        }
        window.addEventListener("reel-timing-api-response", onResponse);
        try {
          window.dispatchEvent(
            new CustomEvent("reel-timing-api-request", {
              detail: {
                reqId: reqId,
                baseUrl: getApiBaseUrl(),
                path: path,
                method: "POST",
                body: body,
              },
            })
          );
        } catch (dispatchErr) {
          settled = true;
          cleanup();
          reject(dispatchErr);
          return;
        }
        setTimeout(function () {
          if (settled) {
            return;
          }
          settled = true;
          cleanup();
          reject(new Error("extension_api_timeout"));
        }, ms);
      });
    }

    function storeDecision(resp) {
      if (!resp || typeof resp.id !== "string" || !resp.id) {
        return;
      }
      var action = resp.action == null ? null : String(resp.action);
      if (action === "null" || action === "none" || action === "") {
        action = null;
      }
      var comment =
        resp.comment == null || resp.comment === ""
          ? null
          : String(resp.comment);
      var duration = Number(resp.duration);
      if (!(duration > 0) || !isFinite(duration)) {
        duration = DEFAULT_WATCH_DURATION_S;
      }
      if (comment && duration < MIN_COMMENT_WATCH_S) {
        duration = MIN_COMMENT_WATCH_S;
      }
      reelDecisionMap.set(resp.id, {
        action: action,
        comment: comment,
        duration: duration,
        receivedAt: performance.now(),
      });
    }

    /**
     * POST entire new-reel batch to Gradio /reels once. Coalesces concurrent callers.
     */
    function requestDecisionsForBatch(codes) {
      var pending = [];
      for (var i = 0; i < codes.length; i++) {
        var code = codes[i];
        if (!code) {
          continue;
        }
        if (reelDecisionMap.has(code) || decisionFetchIds[code]) {
          continue;
        }
        pending.push(code);
      }
      if (!pending.length) {
        return decisionFetchInFlight || Promise.resolve();
      }

      var base = getApiBaseUrl();
      if (!base) {
        logPlan(
          "PLAN_DEVIATION reason=missing_API_BASE_URL — set API_BASE_URL in content-script.js"
        );
        return Promise.resolve();
      }

      // Mark ids before await so a second ingest doesn't double-POST them.
      for (var m = 0; m < pending.length; m++) {
        decisionFetchIds[pending[m]] = true;
      }

      var body = pending.map(function (id) {
        return { id: id };
      });

      var fetchPromise = (async function () {
        var url = base + "/reels";
        logBot(
          "DECISIONS_REQUEST count=" +
            pending.length +
            " url=" +
            url +
            " t=" +
            performance.now()
        );
        try {
          // Prefer extension background fetch (bypasses page CORS / private-network block).
          var data = await extensionApiPost("/reels", body, 15000);
          if (!Array.isArray(data)) {
            throw new Error("response_not_array");
          }
          for (var r = 0; r < data.length; r++) {
            storeDecision(data[r]);
          }
          // Ensure every requested id has an entry (server may omit some).
          for (var p = 0; p < pending.length; p++) {
            if (!reelDecisionMap.has(pending[p])) {
              logPlan(
                "PLAN_DEVIATION reel_id=" +
                  pending[p] +
                  " reason=missing_from_server_response"
              );
              storeDecision({
                id: pending[p],
                action: null,
                comment: null,
                duration: DEFAULT_WATCH_DURATION_S,
              });
            }
          }
          logBot(
            "DECISIONS_RECEIVED count=" +
              data.length +
              " requested=" +
              pending.length +
              " t=" +
              performance.now()
          );
          for (var d = 0; d < data.length; d++) {
            var row = data[d] || {};
            logBot(
              "DECISION id=" +
                row.id +
                " action=" +
                (row.action == null ? "none" : row.action) +
                " comment=" +
                (row.comment == null || row.comment === ""
                  ? "none"
                  : JSON.stringify(row.comment)) +
                " duration=" +
                row.duration
            );
          }
        } catch (err) {
          logPlan(
            "PLAN_DEVIATION reason=decision_fetch_failed error=" +
              (err && err.message) +
              " count=" +
              pending.length
          );
          for (var f = 0; f < pending.length; f++) {
            if (!reelDecisionMap.has(pending[f])) {
              storeDecision({
                id: pending[f],
                action: null,
                comment: null,
                duration: DEFAULT_WATCH_DURATION_S,
              });
            }
          }
        } finally {
          for (var c = 0; c < pending.length; c++) {
            delete decisionFetchIds[pending[c]];
          }
        }
      })();

      // Chain if another batch is already in flight.
      var chained = (decisionFetchInFlight || Promise.resolve())
        .catch(function () {
          /* ignore prior failure */
        })
        .then(function () {
          return fetchPromise;
        })
        .finally(function () {
          if (decisionFetchInFlight === chained) {
            decisionFetchInFlight = null;
          }
        });
      decisionFetchInFlight = chained;
      return chained;
    }

    async function waitForReelDecision(reelId, timeoutMs) {
      if (reelDecisionMap.has(reelId)) {
        return reelDecisionMap.get(reelId);
      }
      var start = performance.now();
      while (performance.now() - start < timeoutMs) {
        if (reelDecisionMap.has(reelId)) {
          return reelDecisionMap.get(reelId);
        }
        if (decisionFetchInFlight) {
          try {
            await Promise.race([
              decisionFetchInFlight,
              sleep(200),
            ]);
          } catch (_e) {
            /* ignore */
          }
        } else {
          await sleep(150);
        }
      }
      return reelDecisionMap.has(reelId)
        ? reelDecisionMap.get(reelId)
        : null;
    }

    function edgesHaveMediaCodes(edges) {
      try {
        if (!Array.isArray(edges) || edges.length === 0) {
          return false;
        }
        for (var i = 0; i < edges.length; i++) {
          var media = edges[i] && edges[i].node && edges[i].node.media;
          if (media && typeof media.code === "string" && media.code) {
            return true;
          }
        }
        return false;
      } catch (_err) {
        return false;
      }
    }

    function deepFindClipsEdges(node, depth) {
      try {
        // RelayPrefetchedStreamCache nests deeply under require/__bbox/result/data.
        if (!node || typeof node !== "object" || depth > 18) {
          return null;
        }
        if (Array.isArray(node)) {
          if (edgesHaveMediaCodes(node)) {
            return node;
          }
          for (var a = 0; a < node.length; a++) {
            var foundArr = deepFindClipsEdges(node[a], depth + 1);
            if (foundArr) {
              return foundArr;
            }
          }
          return null;
        }

        // Fast path: direct clips connection object at this node.
        if (
          node[CLIPS_EDGES_KEY] &&
          Array.isArray(node[CLIPS_EDGES_KEY].edges) &&
          edgesHaveMediaCodes(node[CLIPS_EDGES_KEY].edges)
        ) {
          return node[CLIPS_EDGES_KEY].edges;
        }

        if (Array.isArray(node.edges) && edgesHaveMediaCodes(node.edges)) {
          return node.edges;
        }

        // Prefer descending into known Relay / GraphQL containers first.
        var priorityKeys = ["data", "result", "__bbox", "require", CLIPS_EDGES_KEY];
        for (var p = 0; p < priorityKeys.length; p++) {
          var pk = priorityKeys[p];
          if (node[pk] != null) {
            var foundPri = deepFindClipsEdges(node[pk], depth + 1);
            if (foundPri) {
              return foundPri;
            }
          }
        }

        for (var key in node) {
          if (!Object.prototype.hasOwnProperty.call(node, key)) {
            continue;
          }
          if (priorityKeys.indexOf(key) !== -1) {
            continue;
          }
          var found = deepFindClipsEdges(node[key], depth + 1);
          if (found) {
            return found;
          }
        }
        return null;
      } catch (_err) {
        return null;
      }
    }

    function extractClipsEdges(payload) {
      try {
        if (!payload || typeof payload !== "object") {
          return null;
        }

        // Standard GraphQL network response: { data: { xdt_api__v1__clips__... } }
        var data = payload.data;
        if (data && typeof data === "object") {
          var home = data[CLIPS_EDGES_KEY];
          if (home && Array.isArray(home.edges) && edgesHaveMediaCodes(home.edges)) {
            return home.edges;
          }

          for (var key in data) {
            if (!Object.prototype.hasOwnProperty.call(data, key)) {
              continue;
            }
            if (key === CLIPS_EDGES_KEY) {
              continue;
            }
            var lower = key.toLowerCase();
            if (lower.indexOf("clips") === -1 && lower.indexOf("reel") === -1) {
              continue;
            }
            var conn = data[key];
            if (conn && Array.isArray(conn.edges) && edgesHaveMediaCodes(conn.edges)) {
              return conn.edges;
            }
          }
        }

        // Embedded RelayPrefetchedStreamCache / any nested shape.
        return deepFindClipsEdges(payload, 0);
      } catch (_err) {
        return null;
      }
    }

    function extractReelCodeFromLocation() {
      try {
        var path = (window.location && window.location.pathname) || "";
        var match = path.match(/\/reels?\/([A-Za-z0-9_-]+)/);
        return match ? match[1] : null;
      } catch (_err) {
        return null;
      }
    }

    function extractUsernameFromMedia(media) {
      try {
        if (!media || typeof media !== "object") {
          return null;
        }
        var user = media.user || media.owner;
        if (user && typeof user.username === "string" && user.username) {
          return user.username;
        }
        if (typeof media.username === "string" && media.username) {
          return media.username;
        }
        return null;
      } catch (_err) {
        return null;
      }
    }

    function numOrZero(value) {
      var n = Number(value);
      if (!isFinite(n) || n < 0) {
        return 0;
      }
      return Math.floor(n);
    }

    /** likes / comments / reposts from GraphQL media (Instagram.html paths). */
    function extractCounts(media) {
      if (!media || typeof media !== "object") {
        return { likes: 0, comments: 0, reposts: 0 };
      }
      var reposts = media.media_repost_count;
      if (reposts == null) {
        reposts = media.repost_count;
      }
      // When IG hides like/view counts, like_count is unreliable (often a stub) — store null.
      var likesHidden = media.like_and_view_counts_disabled === true;
      return {
        likes: likesHidden ? null : numOrZero(media.like_count),
        comments: numOrZero(media.comment_count),
        reposts: numOrZero(reposts),
      };
    }

    /**
     * Music label from clips_metadata.
     * Prefer licensed music_info; else original_sound_info title + @artist.
     */
    function extractMusic(media) {
      try {
        var cm = media && media.clips_metadata;
        if (!cm || typeof cm !== "object") {
          return null;
        }

        var mi = cm.music_info;
        if (mi && typeof mi === "object") {
          var asset = mi.music_asset_info || mi;
          var title =
            (asset && (asset.title || asset.music_title)) || null;
          var artist =
            (asset && (asset.display_artist || asset.artist_name)) || null;
          if (title && artist) {
            return String(title) + " · " + String(artist);
          }
          if (title) {
            return String(title);
          }
          if (artist) {
            return String(artist);
          }
        }

        var os = cm.original_sound_info;
        if (os && typeof os === "object") {
          var t =
            typeof os.original_audio_title === "string" && os.original_audio_title
              ? os.original_audio_title
              : null;
          var u =
            os.ig_artist && typeof os.ig_artist.username === "string"
              ? os.ig_artist.username
              : null;
          if (t && u) {
            return t + " · @" + u;
          }
          if (t) {
            return t;
          }
          if (u) {
            return "Original audio · @" + u;
          }
        }
        return null;
      } catch (_err) {
        return null;
      }
    }

    /** Extract slim ingest fields from transient GraphQL media (do not retain media). */
    function ingestFieldsFromMedia(media, username) {
      var counts = extractCounts(media);
      return {
        username: username || "unknown",
        music: extractMusic(media),
        likes: counts.likes,
        comments: counts.comments,
        reposts: counts.reposts,
      };
    }

    function buildIngestRowFromMeta(reelId) {
      var meta = reelMetadataMap.get(reelId);
      if (!meta) {
        return null;
      }
      return {
        id: reelId,
        username: meta.username || "unknown",
        music: meta.music != null ? meta.music : null,
        likes: meta.likes == null ? null : numOrZero(meta.likes),
        comments: numOrZero(meta.comments),
        reposts: numOrZero(meta.reposts),
      };
    }

    /**
     * Fire-and-forget persist. Rows must already be plain copies (safe after memory release).
     */
    function requestIngestBatch(rows) {
      if (!rows || !rows.length) {
        return Promise.resolve();
      }
      var base = getApiBaseUrl();
      if (!base) {
        return Promise.resolve();
      }
      return extensionApiPost("/reels/ingest", rows, 15000)
        .then(function (data) {
          var n =
            data && typeof data.upserted === "number"
              ? data.upserted
              : rows.length;
          logBot("INGEST_OK upserted=" + n + " sent=" + rows.length);
        })
        .catch(function (err) {
          warn(
            "WARN ingest failed: " + ((err && err.message) || String(err))
          );
        });
    }

    function markAutopilotDone(reelId) {
      if (!reelId || autopilotDoneIds[reelId]) {
        return;
      }
      autopilotDoneIds[reelId] = true;
      doneIdRing.push(reelId);
      while (doneIdRing.length > MAX_DONE_TRACK) {
        var old = doneIdRing.shift();
        if (old && old !== reelId) {
          delete autopilotDoneIds[old];
        }
      }
    }

    /** Drop per-reel heavy state after watch/skip. Keeps done marker only. */
    function releaseReelMemory(reelId) {
      if (!reelId) {
        return;
      }
      delete autopilotQueuedIds[reelId];
      reelMetadataMap.delete(reelId);
      reelDecisionMap.delete(reelId);
      reelRunLog.delete(reelId);
      delete decisionFetchIds[reelId];
    }

    /**
     * Compact ordered ids + evict oldest never-watched prefetch when over cap.
     * Never drops the current job or queued reels.
     */
    function pruneReelCaches() {
      try {
        var currentId =
          currentAutopilotReel && currentAutopilotReel.reelId
            ? currentAutopilotReel.reelId
            : null;

        // Compact ordered list to codes still in the map.
        if (orderedReelIds.length > reelMetadataMap.size) {
          var compacted = [];
          for (var c = 0; c < orderedReelIds.length; c++) {
            if (reelMetadataMap.has(orderedReelIds[c])) {
              compacted.push(orderedReelIds[c]);
            }
          }
          orderedReelIds = compacted;
        }

        while (reelMetadataMap.size > MAX_REEL_METADATA && orderedReelIds.length) {
          var victim = null;
          for (var i = 0; i < orderedReelIds.length; i++) {
            var code = orderedReelIds[i];
            if (!reelMetadataMap.has(code)) {
              continue;
            }
            if (code === currentId || autopilotQueuedIds[code]) {
              continue;
            }
            // Prefer done leftovers, then oldest never-viewport (unwatched prefetch).
            var meta = reelMetadataMap.get(code);
            if (autopilotDoneIds[code]) {
              victim = code;
              break;
            }
            if (meta && typeof meta.viewport_entered_at !== "number") {
              victim = code;
              break;
            }
          }
          if (!victim) {
            break;
          }
          releaseReelMemory(victim);
          orderedReelIds = orderedReelIds.filter(function (id) {
            return id !== victim;
          });
        }

        if (viewportReelOrder.length > MAX_DONE_TRACK) {
          viewportReelOrder = viewportReelOrder.slice(-MAX_DONE_TRACK);
        }

        if (observedContainers.length > MAX_OBSERVED_CONTAINERS) {
          var kept = [];
          for (var o = 0; o < observedContainers.length; o++) {
            var el = observedContainers[o];
            if (el && el.isConnected) {
              kept.push(el);
            }
          }
          observedContainers =
            kept.length > MAX_OBSERVED_CONTAINERS
              ? kept.slice(-MAX_OBSERVED_CONTAINERS)
              : kept;
        }

        if (pendingViewports.length > 20) {
          pendingViewports = pendingViewports.slice(-20);
        }
      } catch (_pruneErr) {
        /* ignore */
      }
    }

    /**
     * After the bot finishes a reel: optionally ingest (watched only), then free memory.
     * Builds the row before release so the POST does not need the map entry.
     */
    function finishAutopilotReel(reelId, watched) {
      var row = null;
      if (watched) {
        row = buildIngestRowFromMeta(reelId);
      }
      markAutopilotDone(reelId);
      releaseReelMemory(reelId);
      pruneReelCaches();
      if (row) {
        requestIngestBatch([row]);
      } else if (watched) {
        warn("WARN ingest skipped — no cached metadata reel_id=" + reelId);
      }
    }

    /**
     * Normalize a clips edge into { code, username, media } or null.
     * Ads / netego / placeholders often have null media or no shortcode — skip silently.
     * `media` is transient for ingest field extraction — do not store it in reelMetadataMap.
     */
    function extractReelFromEdge(edge) {
      try {
        if (!edge || typeof edge !== "object") {
          return null;
        }
        var node = edge.node || edge;
        if (!node || typeof node !== "object") {
          return null;
        }

        var media = null;
        if (node.media && typeof node.media === "object") {
          media = node.media;
        } else if (node.clip && node.clip.media && typeof node.clip.media === "object") {
          media = node.clip.media;
        } else if (
          Array.isArray(node.media) &&
          node.media[0] &&
          typeof node.media[0] === "object"
        ) {
          media = node.media[0];
        }

        var code = null;
        if (media) {
          if (typeof media.code === "string" && media.code) {
            code = media.code;
          } else if (typeof media.shortcode === "string" && media.shortcode) {
            code = media.shortcode;
          }
        }
        if (!code && typeof node.code === "string" && node.code) {
          code = node.code;
        }
        if (!code && typeof node.shortcode === "string" && node.shortcode) {
          code = node.shortcode;
        }

        // No shortcode ⇒ not a correlatable reel (ad unit, suggested card, etc.).
        if (!code) {
          return null;
        }

        var username = extractUsernameFromMedia(media);
        if (!username && node.user && typeof node.user.username === "string") {
          username = node.user.username;
        }

        return { code: code, username: username, media: media };
      } catch (_err) {
        return null;
      }
    }

    function ingestGraphqlPayload(payload, sourceLabel) {
      try {
        var edges = extractClipsEdges(payload);
        if (!edges) {
          return;
        }

        // Normalize once: drop ads/netego (no shortcode) without per-edge WARN spam.
        var reelEdges = [];
        var skippedNonReel = 0;
        for (var e = 0; e < edges.length; e++) {
          var extracted = extractReelFromEdge(edges[e]);
          if (!extracted) {
            skippedNonReel += 1;
            continue;
          }
          reelEdges.push(extracted);
        }

        // Cache slim ingest fields in memory only — Supabase write happens after watch.
        // Refresh music/counts on re-seen edges so watch-time ingest is up to date.
        for (var ir = 0; ir < reelEdges.length; ir++) {
          var edgeEx = reelEdges[ir];
          var fields = ingestFieldsFromMedia(edgeEx.media, edgeEx.username);
          // Drop heavy GraphQL media reference ASAP; keep slim copy on the edge.
          edgeEx.media = null;
          edgeEx.ingestFields = fields;
          var existing = reelMetadataMap.get(edgeEx.code);
          if (existing) {
            existing.username = fields.username || existing.username || null;
            existing.music = fields.music;
            existing.likes = fields.likes;
            existing.comments = fields.comments;
            existing.reposts = fields.reposts;
          }
        }

        // Pre-scan: skip decision/metadata work when batch is entirely duplicates.
        var newReels = [];
        for (var pre = 0; pre < reelEdges.length; pre++) {
          if (!reelMetadataMap.has(reelEdges[pre].code)) {
            newReels.push(reelEdges[pre]);
          }
        }
        if (!newReels.length) {
          pruneReelCaches();
          flushPendingViewportMatches();
          return;
        }

        var tBatch = performance.now();
        batchCounter += 1;
        var batchId = batchCounter;
        var source = sourceLabel || "graphql";

        if (batchId > 1) {
          log("=== NEW BATCH #" + batchId + " DETECTED === t=" + tBatch);
        }

        log(
          "METADATA_BATCH_RECEIVED count=" +
            reelEdges.length +
            " new=" +
            newReels.length +
            (skippedNonReel ? " skipped_non_reel=" + skippedNonReel : "") +
            " batch_id=" +
            batchId +
            " source=" +
            source +
            " t=" +
            tBatch
        );

        for (var i = 0; i < newReels.length; i++) {
          try {
            var reel = newReels[i];
            var code = reel.code;

            // Dedupe fetch+XHR+embedded Relay deliveries of the same reel.
            if (reelMetadataMap.has(code)) {
              continue;
            }

            var fieldsNew =
              reel.ingestFields ||
              ingestFieldsFromMedia(reel.media, reel.username);
            reel.media = null;
            var username = fieldsNew.username || reel.username || null;
            var metadataSeenAt = performance.now();
            // Global append order — same index used by VIEWPORT_ENTERED / validation table.
            var globalPosition = orderedReelIds.length;
            reelMetadataMap.set(code, {
              position: globalPosition,
              batch_position: i,
              metadata_seen_at: metadataSeenAt,
              batch_id: batchId,
              username: username,
              music: fieldsNew.music,
              likes: fieldsNew.likes,
              comments: fieldsNew.comments,
              reposts: fieldsNew.reposts,
            });
            orderedReelIds.push(code);

            log(
              "METADATA reel_id=" +
                code +
                " position=" +
                globalPosition +
                " batch_id=" +
                batchId +
                " username=" +
                (username || "N/A") +
                " t=" +
                metadataSeenAt
            );
          } catch (_edgeErr) {
            /* ignore single bad reel; never spam console */
          }
        }

        // Ask decision server for the entire new batch (one POST, not per-id).
        var batchCodes = [];
        for (var b = 0; b < newReels.length; b++) {
          batchCodes.push(newReels[b].code);
        }
        requestDecisionsForBatch(batchCodes);
        pruneReelCaches();

        // Viewport often fires before network GraphQL on deep-linked /reels/<code>/ pages.
        flushPendingViewportMatches();
      } catch (_ingestErr) {
        warn("WARN ingestGraphqlPayload failed (shape may have changed)");
      }
    }

    // --- Embedded Relay prefetch (HTML boot payload) ---
    // Deep-linked /reels/<code>/ pages ship clips edges inside
    // <script type="application/json"> RelayPrefetchedStreamCache blobs — not via /graphql.
    var seenJsonScripts = new WeakSet();

    function considerJsonScript(scriptEl) {
      try {
        if (!scriptEl || scriptEl.tagName !== "SCRIPT" || seenJsonScripts.has(scriptEl)) {
          return;
        }
        var type = (scriptEl.getAttribute("type") || "").toLowerCase();
        if (type !== "application/json") {
          return;
        }

        var text = scriptEl.textContent || scriptEl.innerText || "";
        if (!text) {
          return;
        }

        // Cheap prefilter before JSON.parse on large boot payloads.
        if (
          text.indexOf(CLIPS_EDGES_KEY) === -1 &&
          text.indexOf("clips__home") === -1 &&
          text.indexOf('"code"') === -1
        ) {
          seenJsonScripts.add(scriptEl);
          return;
        }

        // Only parse scripts that look like Relay clips prefetches.
        if (
          text.indexOf(CLIPS_EDGES_KEY) === -1 &&
          text.indexOf("RelayPrefetchedStreamCache") === -1 &&
          text.indexOf("PolarisClips") === -1
        ) {
          seenJsonScripts.add(scriptEl);
          return;
        }

        seenJsonScripts.add(scriptEl);
        var json = maybeParseJsonText(text);
        if (!json) {
          return;
        }

        ingestGraphqlPayload(json, "embedded_relay");
      } catch (_scriptErr) {
        /* never break Instagram */
      }
    }

    function scanEmbeddedRelayScripts(root) {
      try {
        var scope = root && root.querySelectorAll ? root : document;
        if (!scope || typeof scope.querySelectorAll !== "function") {
          return;
        }
        var scripts = scope.querySelectorAll('script[type="application/json"]');
        for (var i = 0; i < scripts.length; i++) {
          considerJsonScript(scripts[i]);
        }
      } catch (_scanErr) {
        warn("WARN scanEmbeddedRelayScripts failed");
      }
    }

    function maybeParseJsonText(text) {
      try {
        if (typeof text !== "string" || !text) {
          return null;
        }
        return JSON.parse(text);
      } catch (_err) {
        return null;
      }
    }

    // --- fetch hook ---
    try {
      var originalFetch = window.fetch;
      if (typeof originalFetch === "function") {
        window.fetch = function () {
          var args = arguments;
          var input = args[0];
          var url = "";
          try {
            if (typeof input === "string") {
              url = input;
            } else if (input && typeof input.url === "string") {
              url = input.url;
            }
          } catch (_urlErr) {
            url = "";
          }

          var promise = originalFetch.apply(this, args);

          if (url.indexOf("/graphql") !== -1) {
            promise
              .then(function (response) {
                try {
                  if (!response || typeof response.clone !== "function") {
                    return;
                  }
                  response
                    .clone()
                    .json()
                    .then(function (data) {
                      ingestGraphqlPayload(data, "fetch");
                    })
                    .catch(function () {
                      /* non-JSON graphql-ish response — ignore */
                    });
                } catch (_cloneErr) {
                  /* never break Instagram */
                }
              })
              .catch(function () {
                /* network failure — page handles it */
              });
          }

          return promise;
        };
      }
    } catch (_fetchHookErr) {
      warn("WARN failed to install fetch hook");
    }

    // --- XHR hook ---
    try {
      var xhrOpen = XMLHttpRequest.prototype.open;
      var xhrSend = XMLHttpRequest.prototype.send;

      XMLHttpRequest.prototype.open = function (method, url) {
        try {
          this.__reelTimingUrl =
            typeof url === "string" ? url : url != null ? String(url) : "";
        } catch (_openErr) {
          this.__reelTimingUrl = "";
        }
        return xhrOpen.apply(this, arguments);
      };

      XMLHttpRequest.prototype.send = function () {
        var xhr = this;
        try {
          xhr.addEventListener("load", function () {
            try {
              var reqUrl = xhr.__reelTimingUrl || "";
              if (reqUrl.indexOf("/graphql") === -1) {
                return;
              }
              var payload = maybeParseJsonText(xhr.responseText);
              if (payload) {
                ingestGraphqlPayload(payload, "xhr");
              }
            } catch (_loadErr) {
              /* ignore */
            }
          });
        } catch (_sendHookErr) {
          /* ignore */
        }
        return xhrSend.apply(this, arguments);
      };
    } catch (_xhrHookErr) {
      warn("WARN failed to install XHR hook");
    }

    /**
     * Profile link in the reel overlay: aria-label="username reels".
     * Video and overlay are siblings under a shared ancestor — finding this
     * anchor forces acceptance to that shared root, not the inner video wrapper.
     */
    function findProfileLinkAnchor(scope) {
      try {
        if (!scope || typeof scope.querySelectorAll !== "function") {
          return null;
        }
        var anchors = scope.querySelectorAll("a[href]");
        for (var i = 0; i < anchors.length; i++) {
          var aria = anchors[i].getAttribute("aria-label") || "";
          if (/^\S+\s+reels$/i.test(aria)) {
            return anchors[i];
          }
        }
        return null;
      } catch (_err) {
        return null;
      }
    }

    /**
     * Validate a candidate reel container before counting it.
     * Requires video + profile-link overlay (not size alone).
     * @returns {{ ok: boolean, reason?: string, profileLinkAnchor?: Element }}
     */
    function isGenuineReelContainer(el) {
      try {
        if (!el || el.nodeType !== 1) {
          return { ok: false, reason: "other" };
        }

        var videos = el.querySelectorAll ? el.querySelectorAll("video") : [];
        if (!videos || videos.length !== 1) {
          return { ok: false, reason: videos && videos.length === 0 ? "no_video" : "other" };
        }

        var profileLink = findProfileLinkAnchor(el);
        if (!profileLink) {
          return { ok: false, reason: "no_profile_link" };
        }

        // Size is a corroborating signal only, not a hard gate.
        var rect = el.getBoundingClientRect();
        var vw = window.innerWidth || document.documentElement.clientWidth || 0;
        var vh = window.innerHeight || document.documentElement.clientHeight || 0;
        if (rect.width < 2 || rect.height < 2) {
          return { ok: false, reason: "not_laid_out" };
        }
        if (vw >= 1 && vh >= 1) {
          var minH = vh * 0.4;
          var minW = vw * 0.25;
          if (rect.height < minH || rect.width < minW) {
            warn(
              "WARN container passed video+profile-link check but is smaller than expected size heuristic (w=" +
                rect.width +
                " h=" +
                rect.height +
                ")"
            );
          }
        }

        return { ok: true, profileLinkAnchor: profileLink };
      } catch (_err) {
        return { ok: false, reason: "other" };
      }
    }

    function resolveReelContainer(videoEl) {
      try {
        if (!videoEl || videoEl.nodeType !== 1) {
          return null;
        }

        // Prefer article only when it already has video + profile overlay.
        // Do NOT early-return on article failure — walk up instead (overlay may
        // live under a non-article shared ancestor above a video-only wrapper).
        if (videoEl.closest) {
          var article = videoEl.closest('article, [role="article"]');
          if (article) {
            var articleCheck = isGenuineReelContainer(article);
            if (articleCheck.ok) {
              return { el: article, check: articleCheck };
            }
          }
        }

        // Walk up: inner xp9pnto fails no_profile_link; shared ancestor passes.
        var node = videoEl.parentElement;
        var fallback = videoEl.parentElement || videoEl;
        var fallbackCheck = { ok: false, reason: "other" };
        var steps = 0;
        while (node && steps < 20) {
          var check = isGenuineReelContainer(node);
          if (check.ok) {
            return { el: node, check: check };
          }
          // Keep walking past video-only wrappers (no_profile_link) until overlay scope.
          fallback = node;
          fallbackCheck = check;
          node = node.parentElement;
          steps += 1;
        }

        return { el: fallback, check: fallbackCheck };
      } catch (_err) {
        return null;
      }
    }

    function extractDomUsername(container) {
      try {
        var anchor = findProfileLinkAnchor(container);
        if (!anchor) {
          return null;
        }

        // Primary: aria-label="username reels"
        var aria = anchor.getAttribute("aria-label") || "";
        var ariaMatch = aria.match(/^(\S+)\s+reels$/i);
        if (ariaMatch) {
          return ariaMatch[1];
        }

        // Fallback: first path segment of href (real hrefs are /username/reels/).
        var href = anchor.getAttribute("href") || "";
        var path = href.split("?")[0].replace(/^\/+|\/+$/g, "");
        if (!path) {
          return null;
        }
        var candidate = path.split("/")[0];
        if (!/^[A-Za-z0-9._]+$/.test(candidate)) {
          return null;
        }
        var reserved = {
          reel: 1,
          reels: 1,
          p: 1,
          explore: 1,
          stories: 1,
          direct: 1,
          accounts: 1,
          about: 1,
          legal: 1,
          privacy: 1,
          tags: 1,
          locations: 1,
        };
        if (reserved[candidate.toLowerCase()]) {
          return null;
        }
        return candidate;
      } catch (_err) {
        return null;
      }
    }

    function containerPosition(container) {
      try {
        if (containerIndexMap.has(container)) {
          return containerIndexMap.get(container);
        }
        return observedContainers.indexOf(container);
      } catch (_err) {
        return -1;
      }
    }

    /**
     * Correlate viewport entry → metadata by username (FIFO).
     * Positional container indexes drift under virtualization; username does not.
     * Picks the earliest not-yet-matched metadata reel with the same username
     * (handles multiple reels from one creator in order).
     */
    function matchMetadataByUsername(domUsername) {
      try {
        if (!domUsername || typeof domUsername !== "string") {
          return null;
        }
        var needle = domUsername.toLowerCase();
        for (var i = 0; i < orderedReelIds.length; i++) {
          var code = orderedReelIds[i];
          var meta = reelMetadataMap.get(code);
          if (!meta) {
            continue;
          }
          if (typeof meta.viewport_entered_at === "number") {
            continue;
          }
          var metaUser = meta.username;
          if (
            metaUser &&
            typeof metaUser === "string" &&
            metaUser.toLowerCase() === needle
          ) {
            return { reelId: code, meta: meta, position: i };
          }
        }
        return null;
      } catch (_err) {
        return null;
      }
    }

    /** Fallback when username isn't in metadata yet: URL shortcode on /reels/<code>/. */
    function matchMetadataByUrlCode() {
      try {
        var code = extractReelCodeFromLocation();
        if (!code || !reelMetadataMap.has(code)) {
          return null;
        }
        var meta = reelMetadataMap.get(code);
        if (!meta || typeof meta.viewport_entered_at === "number") {
          return null;
        }
        return {
          reelId: code,
          meta: meta,
          position: orderedReelIds.indexOf(code),
        };
      } catch (_err) {
        return null;
      }
    }

    function resolveViewportMatch(domUsername) {
      var byUser = matchMetadataByUsername(domUsername);
      if (byUser) {
        return byUser;
      }
      return matchMetadataByUrlCode();
    }

    function commitViewportMatch(matched, domUsername, t, containerEl) {
      var reelId = matched.reelId;
      var meta = matched.meta;
      var position = matched.position;
      var delta = t - meta.metadata_seen_at;
      meta.viewport_entered_at = t;
      meta.delta_ms = delta;
      meta.dom_username = domUsername || "N/A";
      meta.liked = !!meta.liked;
      meta.saved = !!meta.saved;
      meta.commented = !!meta.commented;

      viewportMatchIndex += 1;
      var index = viewportMatchIndex;
      meta.match_index = index;

      logViewport(
        "VIEWPORT_ENTERED reel_id=" +
          reelId +
          " position=" +
          position +
          " index=" +
          index +
          " dom_username=" +
          (domUsername || "N/A") +
          " t=" +
          t +
          " delta_ms=" +
          delta
      );
      if (!QUIET_META_VIEWPORT_LOGS) {
        logState(t);
      }

      viewportHitCount += 1;
      viewportReelOrder.push(reelId);

      // Hand off to autopilot queue (FIFO). Never overwrite — fast scrolls enqueue both.
      enqueueAutopilotJob({
        reelId: reelId,
        containerEl: containerEl || null,
        index: index,
      });
    }

    function enqueueAutopilotJob(job) {
      try {
        if (!job || !job.reelId) {
          return;
        }
        if (autopilotQueuedIds[job.reelId] || autopilotDoneIds[job.reelId]) {
          return;
        }
        autopilotQueuedIds[job.reelId] = true;
        autopilotQueue.push(job);
        logBot(
          "AUTOPILOT_QUEUED reel_id=" +
            job.reelId +
            " index=" +
            job.index +
            " queue=" +
            autopilotQueue.length
        );
        if (typeof autopilotReelWaiter === "function") {
          var wake = autopilotReelWaiter;
          autopilotReelWaiter = null;
          wake();
        }
      } catch (_err) {
        /* ignore */
      }
    }

    function dequeueAutopilotJob(previousReelId) {
      while (autopilotQueue.length) {
        var job = autopilotQueue.shift();
        if (!job) {
          continue;
        }
        if (previousReelId && job.reelId === previousReelId) {
          continue;
        }
        if (autopilotDoneIds[job.reelId]) {
          continue;
        }
        return job;
      }
      return null;
    }

    function flushPendingViewportMatches() {
      try {
        if (!pendingViewports.length) {
          return;
        }
        var remaining = [];
        for (var i = 0; i < pendingViewports.length; i++) {
          var item = pendingViewports[i];
          if (!item || !item.el) {
            continue;
          }
          if (enteredElements.has(item.el)) {
            continue;
          }
          var matched = resolveViewportMatch(item.domUsername);
          if (!matched) {
            remaining.push(item);
            continue;
          }
          enteredElements.add(item.el);
          try {
            pendingElements.delete(item.el);
          } catch (_delErr) {
            /* ignore */
          }
          // Use original viewport timestamp (not ingest time) for accurate delta.
          commitViewportMatch(matched, item.domUsername, item.t, item.el);
        }
        pendingViewports = remaining;
      } catch (_flushErr) {
        warn("WARN flushPendingViewportMatches failed");
      }
    }

    function logState(t) {
      log(
        "STATE total_containers_accepted=" +
          totalContainersAccepted +
          " total_containers_rejected=" +
          totalContainersRejected +
          " total_metadata_reels=" +
          orderedReelIds.length +
          " t=" +
          t
      );
    }

    function printValidationTable() {
      try {
        var rows = [];
        reelMetadataMap.forEach(function (meta, code) {
          if (typeof meta.viewport_entered_at === "number") {
            rows.push({
              reel_id: code,
              position: meta.position,
              index: meta.match_index || "N/A",
              username: meta.username || "N/A",
              dom_username: meta.dom_username || "N/A",
              liked: !!meta.liked,
              saved: !!meta.saved,
              commented: !!meta.commented,
              metadata_seen_at: meta.metadata_seen_at,
              viewport_entered_at: meta.viewport_entered_at,
              delta_ms: meta.delta_ms,
            });
          }
        });
        rows.sort(function (a, b) {
          return a.viewport_entered_at - b.viewport_entered_at;
        });
        log("validation table (" + rows.length + " matched viewport entries)");
        console.table(rows);
      } catch (_tableErr) {
        warn("WARN console.table failed");
      }
    }

    function handleViewportEntered(target) {
      try {
        if (!target || enteredElements.has(target) || pendingElements.has(target)) {
          return;
        }

        try {
          if (intersectionObserver) {
            intersectionObserver.unobserve(target);
          }
        } catch (_unobsErr) {
          /* ignore */
        }

        var t = performance.now();
        var domUsername = extractDomUsername(target);
        // Container discovery index is diagnostic-only (virtualization drifts it).
        var domPosition = containerPosition(target);

        var matched = resolveViewportMatch(domUsername);
        if (!matched) {
          // Race: DOM/viewport often ready before GraphQL metadata on deep links.
          pendingElements.add(target);
          pendingViewports.push({
            el: target,
            domUsername: domUsername,
            t: t,
            domPosition: domPosition,
          });
          if (!QUIET_META_VIEWPORT_LOGS) {
            warn(
              "WARN viewport pending metadata at position=" +
                domPosition +
                " dom_username=" +
                (domUsername || "N/A") +
                " t=" +
                t
            );
            logState(t);
          }
          return;
        }

        enteredElements.add(target);
        commitViewportMatch(matched, domUsername, t, target);
      } catch (_entryErr) {
        warn("WARN viewport entry handler failed");
      }
    }

    function isAlreadyMostlyInViewport(el) {
      try {
        var rect = el.getBoundingClientRect();
        var vw = window.innerWidth || document.documentElement.clientWidth || 0;
        var vh = window.innerHeight || document.documentElement.clientHeight || 0;
        if (vw < 1 || vh < 1 || rect.width < 1 || rect.height < 1) {
          return false;
        }

        var visibleLeft = Math.max(rect.left, 0);
        var visibleTop = Math.max(rect.top, 0);
        var visibleRight = Math.min(rect.right, vw);
        var visibleBottom = Math.min(rect.bottom, vh);
        var visibleW = Math.max(0, visibleRight - visibleLeft);
        var visibleH = Math.max(0, visibleBottom - visibleTop);
        var visibleArea = visibleW * visibleH;
        var totalArea = rect.width * rect.height;
        if (totalArea <= 0) {
          return false;
        }
        return visibleArea / totalArea >= 0.5;
      } catch (_err) {
        return false;
      }
    }

    var intersectionObserver = null;
    try {
      intersectionObserver = new IntersectionObserver(
        function (entries) {
          for (var e = 0; e < entries.length; e++) {
            try {
              var entry = entries[e];
              if (!entry || !entry.isIntersecting) {
                continue;
              }
              handleViewportEntered(entry.target);
            } catch (_entryErr) {
              warn("WARN viewport entry handler failed");
            }
          }
        },
        { threshold: 0.5 }
      );
    } catch (_ioErr) {
      warn("WARN IntersectionObserver unavailable");
    }

    function rejectContainer(el, reason) {
      try {
        if (!el || rejectedContainers.has(el)) {
          return;
        }
        // Temporary — overlay/layout may appear on a later mutation/scan.
        // (Profile link is a sibling of the video wrapper and often mounts later.)
        if (reason === "not_laid_out" || reason === "no_profile_link") {
          return;
        }
        rejectedContainers.add(el);
        totalContainersRejected += 1;
        log(
          "CONTAINER_REJECTED reason=" + reason + " t=" + performance.now()
        );
      } catch (_rejErr) {
        /* ignore */
      }
    }

    function watchContainer(container) {
      try {
        if (!container || seenContainers.has(container) || rejectedContainers.has(container)) {
          return;
        }

        var check = isGenuineReelContainer(container);
        if (!check.ok) {
          if (check.reason === "not_laid_out") {
            // Retry on later mutations / scans once Instagram finishes layout.
            return;
          }
          rejectContainer(container, check.reason || "other");
          return;
        }

        seenContainers.add(container);
        var index = observedContainers.length;
        observedContainers.push(container);
        containerIndexMap.set(container, index);
        totalContainersAccepted += 1;

        if (intersectionObserver) {
          intersectionObserver.observe(container);
        }

        // IntersectionObserver may never fire for elements already intersecting.
        if (isAlreadyMostlyInViewport(container)) {
          handleViewportEntered(container);
        }
      } catch (_watchErr) {
        /* ignore */
      }
    }

    function considerVideo(videoEl) {
      try {
        var resolved = resolveReelContainer(videoEl);
        if (!resolved || !resolved.el) {
          return;
        }
        if (!resolved.check || !resolved.check.ok) {
          var reason =
            (resolved.check && resolved.check.reason) || "other";
          // Retry later: overlay (profile link) often mounts after the video wrapper.
          if (reason === "not_laid_out" || reason === "no_profile_link") {
            return;
          }
          rejectContainer(resolved.el, reason);
          return;
        }
        watchContainer(resolved.el);
      } catch (_err) {
        /* ignore */
      }
    }

    function scanForVideos(root) {
      try {
        if (!root || typeof root.querySelectorAll !== "function") {
          return;
        }
        var videos = root.querySelectorAll("video");
        for (var i = 0; i < videos.length; i++) {
          considerVideo(videos[i]);
        }
      } catch (_scanErr) {
        /* ignore */
      }
    }

    function onMutations(mutations) {
      try {
        for (var m = 0; m < mutations.length; m++) {
          var mutation = mutations[m];
          var added = mutation.addedNodes;
          if (!added) {
            continue;
          }
          for (var n = 0; n < added.length; n++) {
            var node = added[n];
            if (!node || node.nodeType !== 1) {
              continue;
            }
            if (node.tagName === "SCRIPT") {
              considerJsonScript(node);
            }
            if (node.tagName === "VIDEO") {
              considerVideo(node);
            }
            if (typeof node.querySelectorAll === "function") {
              // Catch JSON scripts injected with surrounding markup.
              var nestedScripts = node.querySelectorAll(
                'script[type="application/json"]'
              );
              for (var s = 0; s < nestedScripts.length; s++) {
                considerJsonScript(nestedScripts[s]);
              }
              scanForVideos(node);
            }
          }
        }
      } catch (_mutErr) {
        /* ignore */
      }
    }

    // =========================================================================
    // AUTOPILOT: human-like watch → engage → scroll
    // =========================================================================

    function sleep(ms) {
      return new Promise(function (resolve) {
        setTimeout(resolve, ms);
      });
    }

    /** Sleep until at least `ms` have elapsed (tops up short setTimeout drift). */
    async function sleepExact(ms) {
      var target = Math.max(0, Math.round(ms));
      var start = performance.now();
      var remaining = target - (performance.now() - start);
      while (remaining > 2) {
        await sleep(Math.min(remaining, 250));
        remaining = target - (performance.now() - start);
      }
      // Spin the last couple ms for tighter accuracy.
      while (performance.now() - start < target) {
        /* busy-wait */
      }
    }

    function randBetween(minMs, maxMs) {
      return Math.floor(minMs + Math.random() * (maxMs - minMs + 1));
    }

    function waitForNextAutopilotReel(previousReelId) {
      return new Promise(function (resolve) {
        function tryPull() {
          var job = dequeueAutopilotJob(previousReelId);
          if (job) {
            currentAutopilotReel = job;
            resolve(job);
            return;
          }
          autopilotReelWaiter = tryPull;
        }
        tryPull();
      });
    }

    function findAriaControl(scope, labelExact) {
      try {
        if (!scope || typeof scope.querySelector !== "function") {
          return null;
        }
        var el = scope.querySelector('[aria-label="' + labelExact + '"]');
        if (!el) {
          return null;
        }
        return (
          el.closest("button, [role='button'], a, div[tabindex]") || el
        );
      } catch (_err) {
        return null;
      }
    }

    function getActiveVideoRect(container) {
      var video = getReelVideo(container);
      if (video) {
        var vr = video.getBoundingClientRect();
        if (vr.width > 1 && vr.height > 1) {
          return vr;
        }
      }
      // Fallback: use the matched reel container itself.
      try {
        if (container && container.getBoundingClientRect) {
          return container.getBoundingClientRect();
        }
      } catch (_err) {
        /* ignore */
      }
      return null;
    }

    /**
     * Score a candidate engage control against the ACTIVE reel video.
     * Returns null if the control clearly belongs to another reel / rail.
     */
    function scoreEngageCandidate(el, vRect) {
      if (!el || !vRect) {
        return null;
      }
      var clickable =
        el.closest("button, [role='button'], a, div[tabindex]") || el;
      var r = clickable.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) {
        return null;
      }
      // Must be on-screen-ish.
      if (r.bottom < 0 || r.top > window.innerHeight) {
        return null;
      }
      var cx = r.left + r.width / 2;
      var cy = r.top + r.height / 2;
      var vcx = vRect.left + vRect.width / 2;
      var vcy = vRect.top + vRect.height / 2;
      var vTop = vRect.top;
      var vBottom = vRect.bottom;

      // Reject controls vertically outside the active reel band.
      // Neighboring virtualized reels' Unlike/Saved were causing false skips.
      // Save sits near the bottom of the action stack — keep pad generous.
      var bandPad = Math.max(140, vRect.height * 0.25);
      if (cy < vTop - bandPad || cy > vBottom + bandPad) {
        return null;
      }

      // Light vertical weight: Save is farthest from video center in the column.
      var dist = Math.abs(cy - vcy) * 1.35 + Math.abs(cx - vcx);
      if (cx < vcx - 40) {
        dist += 800; // left-rail / unrelated UI
      }
      // Soft preference for right-side action column.
      if (cx > vcx) {
        dist -= 40;
      }
      return { el: clickable, dist: dist, cx: cx, cy: cy };
    }

    function bestLabeledNearVideo(labelExact, vRect, root) {
      var scope = root || document;
      var candidates =
        typeof scope.querySelectorAll === "function"
          ? scope.querySelectorAll('[aria-label="' + labelExact + '"]')
          : [];
      var best = null;
      var bestDist = Infinity;
      for (var i = 0; i < candidates.length; i++) {
        var scored = scoreEngageCandidate(candidates[i], vRect);
        if (!scored) {
          continue;
        }
        if (scored.dist < bestDist) {
          bestDist = scored.dist;
          best = scored;
        }
      }
      return best;
    }

    /**
     * Locate the right-side action column for THIS reel by anchoring on
     * Comment (always present, mid-stack). Save is lowest and often fails a
     * pure video-center distance search without this scope.
     */
    function findActionColumnRoot(container) {
      var vRect = getActiveVideoRect(container);
      if (!vRect) {
        return null;
      }
      var anchorLabels = ["Comment", "Like", "Unlike", "Share", "Repost"];
      var anchor = null;
      for (var a = 0; a < anchorLabels.length; a++) {
        var hit = bestLabeledNearVideo(anchorLabels[a], vRect, document);
        if (hit && (!anchor || hit.dist < anchor.dist)) {
          anchor = hit;
        }
      }
      if (!anchor) {
        return null;
      }

      // Walk up until the root likely contains the whole action stack
      // (Like + Comment + Save) but not multiple reels.
      var node = anchor.el.parentElement;
      for (var up = 0; up < 6 && node; up++) {
        var hasComment = !!node.querySelector(
          '[aria-label="Comment"], [aria-label="Like"], [aria-label="Unlike"]'
        );
        var saveOrShare = !!node.querySelector(
          '[aria-label="Save"], [aria-label="Saved"], [aria-label="Unsave"], [aria-label="Share"]'
        );
        var rect = node.getBoundingClientRect();
        var narrow = rect.width > 0 && rect.width < Math.max(120, vRect.width * 0.45);
        if (hasComment && saveOrShare && narrow) {
          return node;
        }
        // Stop before taking over the full reel / page shell.
        if (rect.height > vRect.height * 1.35 && rect.width > vRect.width * 0.7) {
          break;
        }
        node = node.parentElement;
      }
      return anchor.el.parentElement || anchor.el;
    }

    /**
     * Like/Comment/Save often live in a sibling action column OUTSIDE the
     * video+caption container. Prefer the active reel's action column, then
     * nearest-to-video — never the first match in a shared ancestor.
     */
    function findEngageControl(container, labelExact) {
      try {
        var vRect = getActiveVideoRect(container);
        if (!vRect) {
          return null;
        }

        var column = findActionColumnRoot(container);
        var best = null;
        if (column) {
          best = bestLabeledNearVideo(labelExact, vRect, column);
        }
        if (!best) {
          best = bestLabeledNearVideo(labelExact, vRect, document);
        }
        if (!best) {
          return null;
        }

        // Hard cap: allow full action-stack height (Save is bottom-most).
        var cap = Math.max(900, vRect.height * 1.6);
        if (best.dist > cap) {
          return null;
        }
        return best.el;
      } catch (_err) {
        return null;
      }
    }

    /**
     * Toggle state for Like/Save: compare off-label vs on-label inside the
     * active reel action column. Avoids neighbor reel false positives.
     */
    function isEngageStateActive(container, offLabels, onLabels) {
      var vRect = getActiveVideoRect(container);
      if (!vRect) {
        return false;
      }
      var column = findActionColumnRoot(container);
      var root = column || document;

      function bestDistFor(labels) {
        var best = Infinity;
        var found = null;
        for (var i = 0; i < labels.length; i++) {
          var hit = bestLabeledNearVideo(labels[i], vRect, root);
          if (hit && hit.dist < best) {
            best = hit.dist;
            found = hit.el;
          }
        }
        // If column search missed, fall back to document band scoring.
        if (!found && root !== document) {
          for (var j = 0; j < labels.length; j++) {
            var hit2 = bestLabeledNearVideo(labels[j], vRect, document);
            if (hit2 && hit2.dist < best) {
              best = hit2.dist;
              found = hit2.el;
            }
          }
        }
        return { el: found, dist: best };
      }

      var on = bestDistFor(onLabels);
      var off = bestDistFor(offLabels);

      if (!on.el) {
        return false;
      }
      // Active only if the ON control is nearer than any OFF control
      // (or OFF is missing entirely for this reel band).
      if (!off.el) {
        return true;
      }
      return on.dist + 25 < off.dist;
    }

    function isReelAlreadyLiked(container) {
      return isEngageStateActive(container, ["Like"], ["Unlike"]);
    }

    /** Bookmark ribbon geometry used by IG web Save icon (locale-stable). */
    var SAVE_BOOKMARK_POINTS = "20 21 12 13.44 4 21 4 3 20 3 20 21";

    function getSaveSvgFromControl(control) {
      if (!control) {
        return null;
      }
      try {
        if (
          control.tagName === "SVG" ||
          (control.tagName && control.tagName.toLowerCase() === "svg")
        ) {
          return control;
        }
        var byLabel =
          control.querySelector('svg[aria-label="Save"]') ||
          control.querySelector('svg[aria-label="Saved"]') ||
          control.querySelector('svg[aria-label="Unsave"]') ||
          control.querySelector('svg[aria-label="Remove"]');
        if (byLabel) {
          return byLabel;
        }
        var polys = control.querySelectorAll("polygon");
        for (var i = 0; i < polys.length; i++) {
          var pts = (polys[i].getAttribute("points") || "").replace(/\s+/g, " ");
          if (pts.indexOf("20 21") !== -1 && pts.indexOf("12 13") !== -1) {
            return polys[i].ownerSVGElement || polys[i].closest("svg");
          }
        }
      } catch (_err) {
        /* ignore */
      }
      return null;
    }

    function snapshotSaveIcon(control) {
      var svg = getSaveSvgFromControl(control);
      if (!svg) {
        return { label: "", html: "", outline: true };
      }
      var label = (
        (svg.getAttribute("aria-label") ||
          (control.getAttribute && control.getAttribute("aria-label")) ||
          "") + ""
      )
        .trim()
        .toLowerCase();
      var html = "";
      try {
        html = svg.innerHTML || "";
      } catch (_h) {
        html = "";
      }
      var outline = false;
      try {
        outline = !!svg.querySelector(
          'polygon[fill="none"], path[fill="none"], line[fill="none"]'
        );
        // Unsaved bookmark in Instagram.html is stroked polygon fill=none.
        if (!outline) {
          var poly = svg.querySelector('polygon[points*="20 21"]');
          if (poly) {
            var fill = (poly.getAttribute("fill") || "").toLowerCase();
            outline = fill === "none";
          }
        }
      } catch (_o) {
        outline = true;
      }
      return { label: label, html: html, outline: outline };
    }

    /**
     * Instagram.html: unsaved = <polygon fill="none" … stroke…>
     * Saved: filled path/polygon and/or aria-label Remove/Saved — or SVG markup change.
     */
    function isSaveControlInSavedState(control) {
      if (!control) {
        return false;
      }
      var snap = snapshotSaveIcon(control);
      if (
        snap.label === "saved" ||
        snap.label === "unsave" ||
        snap.label === "remove" ||
        snap.label.indexOf("remove from") !== -1
      ) {
        return true;
      }
      // Filled bookmark (no outline stroke polygon) ⇒ saved.
      if (snap.html && !snap.outline) {
        return true;
      }
      return false;
    }

    function saveIconChanged(before, after) {
      if (!before || !after) {
        return false;
      }
      if (before.label !== after.label) {
        return true;
      }
      if (before.outline && !after.outline) {
        return true;
      }
      if (before.html && after.html && before.html !== after.html) {
        return true;
      }
      return false;
    }

    function pageHasSavedToast() {
      try {
        var nodes = document.querySelectorAll(
          '[role="alert"], [role="status"], div[class*="toast"], span'
        );
        for (var i = 0; i < Math.min(nodes.length, 80); i++) {
          var t = (nodes[i].textContent || "").trim().toLowerCase();
          if (!t || t.length > 80) {
            continue;
          }
          if (t === "saved" || t.indexOf("saved") === 0) {
            return true;
          }
        }
      } catch (_err) {
        /* ignore */
      }
      return false;
    }

    async function handleSaveCollectionDialog() {
      try {
        var dialogs = document.querySelectorAll('div[role="dialog"]');
        for (var d = 0; d < dialogs.length; d++) {
          var dialog = dialogs[d];
          var text = (dialog.textContent || "").toLowerCase();
          if (
            text.indexOf("collection") === -1 &&
            text.indexOf("save to") === -1
          ) {
            continue;
          }
          // Prefer an explicit Save / Done control inside the dialog.
          var buttons = dialog.querySelectorAll(
            'button, [role="button"], div[tabindex="0"]'
          );
          for (var b = 0; b < buttons.length; b++) {
            var label = (
              (buttons[b].getAttribute("aria-label") || "") +
              " " +
              (buttons[b].textContent || "")
            )
              .trim()
              .toLowerCase();
            if (
              label === "done" ||
              label === "save" ||
              label.indexOf("save") === 0
            ) {
              humanPointerClick(buttons[b]);
              await sleep(500);
              return true;
            }
          }
          // Fallback: dismiss.
          var closeBtn = findAriaControl(dialog, "Close");
          if (closeBtn) {
            humanPointerClick(closeBtn);
            await sleep(400);
            return true;
          }
        }
      } catch (_err) {
        /* ignore */
      }
      return false;
    }

    function findSaveControl(container) {
      var btn =
        findEngageControl(container, "Save") ||
        findEngageControl(container, "Saved") ||
        findEngageControl(container, "Unsave");
      if (btn) {
        return btn;
      }

      // Locale / DOM fallback: bookmark polygon geometry from Instagram.html.
      try {
        var vRect = getActiveVideoRect(container);
        if (!vRect) {
          return null;
        }
        var column = findActionColumnRoot(container);
        var root = column || document;
        var polys = root.querySelectorAll(
          'polygon[points*="20 21"], polygon[points*="12 13.44"]'
        );
        var best = null;
        var bestDist = Infinity;
        for (var i = 0; i < polys.length; i++) {
          var pts = (polys[i].getAttribute("points") || "").replace(/\s+/g, " ");
          if (pts.indexOf(SAVE_BOOKMARK_POINTS) === -1 && pts.indexOf("20 21") === -1) {
            continue;
          }
          var svg = polys[i].ownerSVGElement || polys[i].closest("svg") || polys[i];
          var scored = scoreEngageCandidate(svg, vRect);
          if (scored && scored.dist < bestDist) {
            bestDist = scored.dist;
            best = scored.el;
          }
        }
        return best;
      } catch (_err) {
        return null;
      }
    }

    function isReelAlreadySaved(container) {
      var control = findSaveControl(container);
      if (!control) {
        return false;
      }
      return isSaveControlInSavedState(control);
    }

    async function waitForSaveState(container, wantSaved, timeoutMs) {
      var start = performance.now();
      while (performance.now() - start < timeoutMs) {
        var control = findSaveControl(container);
        var saved = isSaveControlInSavedState(control);
        if (wantSaved ? saved : !saved && control) {
          return true;
        }
        await sleep(150);
      }
      return false;
    }

    function elementCenter(el) {
      var r = el.getBoundingClientRect();
      return {
        x: r.left + r.width / 2,
        y: r.top + r.height / 2,
        rect: r,
      };
    }

    function buildPointerOpts(x, y, extra) {
      var opts = {
        bubbles: true,
        cancelable: true,
        view: window,
        clientX: x,
        clientY: y,
        screenX: x + (window.screenX || 0),
        screenY: y + (window.screenY || 0),
        button: 0,
        buttons: 1,
        pointerId: 1,
        pointerType: "mouse",
        isPrimary: true,
      };
      if (extra) {
        for (var k in extra) {
          if (Object.prototype.hasOwnProperty.call(extra, k)) {
            opts[k] = extra[k];
          }
        }
      }
      return opts;
    }

    /**
     * Coordinate-based pointer click (hover → press → release).
     * Still isTrusted=false, but hits React/hover handlers more reliably.
     */
    function humanPointerClick(el) {
      try {
        if (!el) {
          return false;
        }
        var center = elementCenter(el);
        if (center.rect.width < 1 || center.rect.height < 1) {
          return false;
        }
        // Small jitter like a human thumb/mouse.
        var x = center.x + (Math.random() * 4 - 2);
        var y = center.y + (Math.random() * 4 - 2);
        var over = buildPointerOpts(x, y, { buttons: 0 });
        var down = buildPointerOpts(x, y, { buttons: 1 });
        var up = buildPointerOpts(x, y, { buttons: 0 });

        el.dispatchEvent(new PointerEvent("pointerover", over));
        el.dispatchEvent(new MouseEvent("mouseover", over));
        el.dispatchEvent(new PointerEvent("pointerenter", over));
        el.dispatchEvent(new MouseEvent("mouseenter", over));
        el.dispatchEvent(new PointerEvent("pointermove", over));
        el.dispatchEvent(new MouseEvent("mousemove", over));
        el.dispatchEvent(new PointerEvent("pointerdown", down));
        el.dispatchEvent(new MouseEvent("mousedown", down));
        el.dispatchEvent(new PointerEvent("pointerup", up));
        el.dispatchEvent(new MouseEvent("mouseup", up));
        el.dispatchEvent(new MouseEvent("click", up));
        // Do NOT also call el.click() — double-fire toggles Like/Save off.
        return true;
      } catch (_err) {
        try {
          if (el && typeof el.click === "function") {
            el.click();
            return true;
          }
        } catch (_e2) {
          /* ignore */
        }
        return false;
      }
    }

    function setNativeInputValue(inputEl, value) {
      try {
        var proto =
          inputEl instanceof HTMLTextAreaElement
            ? HTMLTextAreaElement.prototype
            : HTMLInputElement.prototype;
        var desc = Object.getOwnPropertyDescriptor(proto, "value");
        if (desc && desc.set) {
          desc.set.call(inputEl, value);
        } else {
          inputEl.value = value;
        }
      } catch (_err) {
        inputEl.value = value;
      }
    }

    function getComposerText(inputEl) {
      try {
        if (!inputEl) {
          return "";
        }
        if (inputEl.isContentEditable) {
          return (inputEl.textContent || "").trim();
        }
        return String(inputEl.value || "").trim();
      } catch (_err) {
        return "";
      }
    }

    function isComposerFocused(inputEl) {
      try {
        if (!inputEl) {
          return false;
        }
        var active = document.activeElement;
        if (!active) {
          return false;
        }
        return (
          active === inputEl ||
          inputEl.contains(active) ||
          active.contains(inputEl)
        );
      } catch (_err) {
        return false;
      }
    }

    /** Place caret at end so IG treats the composer as actively editing. */
    function placeComposerCaret(inputEl) {
      try {
        if (!inputEl) {
          return;
        }
        if (inputEl.isContentEditable) {
          var range = document.createRange();
          range.selectNodeContents(inputEl);
          range.collapse(false);
          var sel = window.getSelection();
          if (sel) {
            sel.removeAllRanges();
            sel.addRange(range);
          }
          return;
        }
        if (typeof inputEl.setSelectionRange === "function") {
          var len = String(inputEl.value || "").length;
          inputEl.setSelectionRange(len, len);
        }
      } catch (_err) {
        /* ignore */
      }
    }

    /**
     * Ensure the comment textbox actually has focus before type / Enter / Post.
     * Without this, text can appear while IG never enables Post (and Enter is ignored).
     */
    async function focusComposerInput(inputEl, opts) {
      var withClick = !opts || opts.click !== false;
      if (!inputEl || !inputEl.isConnected) {
        return false;
      }
      try {
        if (typeof window.focus === "function") {
          window.focus();
        }
      } catch (_wf) {
        /* ignore */
      }

      if (withClick) {
        humanPointerClick(inputEl);
        await sleep(randBetween(120, 280));
      }

      try {
        inputEl.focus({ preventScroll: true });
      } catch (_f1) {
        try {
          inputEl.focus();
        } catch (_f2) {
          /* ignore */
        }
      }

      try {
        inputEl.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
        inputEl.dispatchEvent(new FocusEvent("focus", { bubbles: false }));
      } catch (_fe) {
        /* ignore */
      }

      placeComposerCaret(inputEl);
      await sleep(randBetween(80, 180));

      if (isComposerFocused(inputEl)) {
        return true;
      }

      // Second try: click again then focus (dialogs sometimes steal focus once).
      humanPointerClick(inputEl);
      await sleep(randBetween(150, 320));
      try {
        inputEl.focus();
      } catch (_f3) {
        /* ignore */
      }
      placeComposerCaret(inputEl);
      await sleep(100);
      return isComposerFocused(inputEl);
    }

    /**
     * Re-focus + re-fire input so Instagram enables the Post control after typing.
     */
    async function nudgeComposerForPost(getInputFn) {
      var inputEl = typeof getInputFn === "function" ? getInputFn() : getInputFn;
      if (!inputEl) {
        return false;
      }
      var focused = await focusComposerInput(inputEl, { click: true });
      inputEl = typeof getInputFn === "function" ? getInputFn() : inputEl;
      if (!inputEl) {
        return false;
      }
      var text = getComposerText(inputEl);
      if (text) {
        dispatchInputEvents(inputEl, text, "insertText");
      }
      placeComposerCaret(inputEl);
      await sleep(randBetween(200, 450));
      return focused || isComposerFocused(inputEl);
    }

    function dispatchInputEvents(inputEl, data, inputType) {
      try {
        if (typeof InputEvent === "function") {
          inputEl.dispatchEvent(
            new InputEvent("input", {
              bubbles: true,
              cancelable: true,
              data: data,
              inputType: inputType || "insertText",
            })
          );
        } else {
          inputEl.dispatchEvent(new Event("input", { bubbles: true }));
        }
      } catch (_err) {
        inputEl.dispatchEvent(new Event("input", { bubbles: true }));
      }
      try {
        inputEl.dispatchEvent(new Event("change", { bubbles: true }));
      } catch (_c) {
        /* ignore */
      }
    }

    /**
     * Type into Instagram's comment <input>. Re-queries node each char (remounts),
     * tries insertText / native setter / InputEvent, verifies final value.
     * Keeps the composer focused — required for Post to appear.
     */
    async function typeIntoInputHuman(getInputFn, text) {
      var inputEl = typeof getInputFn === "function" ? getInputFn() : getInputFn;
      if (!inputEl) {
        return false;
      }

      await focusComposerInput(inputEl, { click: true });
      await sleep(randBetween(200, 500));

      // Clear
      inputEl = typeof getInputFn === "function" ? getInputFn() : inputEl;
      if (!inputEl) {
        return false;
      }
      await focusComposerInput(inputEl, { click: false });
      try {
        if (inputEl.isContentEditable) {
          inputEl.textContent = "";
        } else {
          setNativeInputValue(inputEl, "");
        }
        dispatchInputEvents(inputEl, "", "deleteContentBackward");
      } catch (_clear) {
        /* ignore */
      }

      for (var i = 0; i < text.length; i++) {
        var ch = text.charAt(i);
        inputEl = typeof getInputFn === "function" ? getInputFn() : inputEl;
        if (!inputEl) {
          return false;
        }
        if (!isComposerFocused(inputEl)) {
          await focusComposerInput(inputEl, { click: true });
        } else {
          try {
            inputEl.focus();
          } catch (_ff) {
            /* ignore */
          }
          placeComposerCaret(inputEl);
        }

        var keyOpts = {
          key: ch,
          code: ch.length === 1 ? "Key" + ch.toUpperCase() : ch,
          keyCode: ch.toUpperCase().charCodeAt(0),
          which: ch.toUpperCase().charCodeAt(0),
          bubbles: true,
          cancelable: true,
        };
        inputEl.dispatchEvent(new KeyboardEvent("keydown", keyOpts));
        inputEl.dispatchEvent(new KeyboardEvent("keypress", keyOpts));

        var inserted = false;
        try {
          if (document.execCommand) {
            inserted = document.execCommand("insertText", false, ch);
          }
        } catch (_ex) {
          inserted = false;
        }

        if (!inserted) {
          if (inputEl.isContentEditable) {
            inputEl.textContent = (inputEl.textContent || "") + ch;
          } else {
            setNativeInputValue(inputEl, (inputEl.value || "") + ch);
          }
        }
        dispatchInputEvents(inputEl, ch, "insertText");
        inputEl.dispatchEvent(new KeyboardEvent("keyup", keyOpts));

        var delay = randBetween(90, 300);
        if (Math.random() < 0.28) {
          delay += randBetween(350, 900);
        }
        await sleep(delay);
      }

      inputEl = typeof getInputFn === "function" ? getInputFn() : inputEl;
      var finalText = getComposerText(inputEl);
      if (finalText.indexOf(text) === -1) {
        // Last-resort bulk set + input event (still under focus)
        try {
          if (inputEl) {
            await focusComposerInput(inputEl, { click: true });
            if (inputEl.isContentEditable) {
              inputEl.textContent = text;
            } else {
              setNativeInputValue(inputEl, text);
            }
            dispatchInputEvents(inputEl, text, "insertText");
            placeComposerCaret(inputEl);
          }
        } catch (_bulk) {
          /* ignore */
        }
        inputEl = typeof getInputFn === "function" ? getInputFn() : inputEl;
        finalText = getComposerText(inputEl);
      }
      return finalText.indexOf(text) !== -1;
    }

    function findCommentComposerInDialog(dialog) {
      if (!dialog || typeof dialog.querySelector !== "function") {
        return null;
      }
      return (
        dialog.querySelector('input[placeholder^="Add a comment"]') ||
        dialog.querySelector('textarea[placeholder^="Add a comment"]') ||
        dialog.querySelector('input[placeholder*="comment"]') ||
        dialog.querySelector('[contenteditable="true"]')
      );
    }

    function findLiveCommentComposer() {
      var dialogs = document.querySelectorAll('div[role="dialog"]');
      for (var d = 0; d < dialogs.length; d++) {
        var input = findCommentComposerInDialog(dialogs[d]);
        if (input && input.isConnected) {
          return { dialog: dialogs[d], input: input };
        }
      }
      return null;
    }

    /**
     * Comment-box open detection (grounded in comment.html):
     * After clicking Comment, Instagram mounts a role="dialog" that contains
     * <input placeholder="Add a comment…">. We require BOTH — not just any dialog.
     */
    async function waitForCommentComposer(timeoutMs) {
      var start = performance.now();
      while (performance.now() - start < timeoutMs) {
        var found = findLiveCommentComposer();
        if (found) {
          return found;
        }
        await sleep(150);
      }
      return null;
    }

    function findPostButton(dialog) {
      if (!dialog) {
        return null;
      }
      var candidates = dialog.querySelectorAll(
        'button, [role="button"], div[tabindex="0"], div[tabindex="-1"]'
      );
      for (var p = 0; p < candidates.length; p++) {
        var c = candidates[p];
        var aria = (c.getAttribute("aria-label") || "").trim().toLowerCase();
        var text = (c.textContent || "").trim().toLowerCase();
        if (aria === "emoji" || text === "emoji") {
          continue;
        }
        if (aria === "post" || text === "post") {
          var disabled =
            c.getAttribute("aria-disabled") === "true" ||
            c.hasAttribute("disabled") ||
            (c.className && String(c.className).indexOf("disabled") !== -1);
          if (!disabled) {
            return c;
          }
        }
      }
      return null;
    }

    async function waitForPostButton(dialog, timeoutMs) {
      var start = performance.now();
      while (performance.now() - start < timeoutMs) {
        var live = findLiveCommentComposer();
        var scope = (live && live.dialog) || dialog;
        var btn = findPostButton(scope);
        if (btn) {
          return btn;
        }
        await sleep(200);
      }
      return null;
    }

    async function waitForCommentSubmitted(timeoutMs) {
      var start = performance.now();
      while (performance.now() - start < timeoutMs) {
        var live = findLiveCommentComposer();
        if (!live) {
          // Dialog closed — likely submitted or dismissed.
          return true;
        }
        var text = getComposerText(live.input);
        if (!text) {
          // Composer cleared after successful post (common IG pattern).
          return true;
        }
        // Post control gone while dialog still open also indicates submit.
        if (!findPostButton(live.dialog) && text) {
          await sleep(400);
          live = findLiveCommentComposer();
          if (!live || !getComposerText(live.input)) {
            return true;
          }
        }
        await sleep(200);
      }
      return false;
    }

    function getReelVideo(container) {
      try {
        if (container && typeof container.querySelector === "function") {
          var local = container.querySelector("video");
          if (local) {
            return local;
          }
        }
        // Prefer the most viewport-centered video (not document.querySelector's first).
        var videos = document.querySelectorAll("video");
        var best = null;
        var bestScore = Infinity;
        var midY = window.innerHeight / 2;
        var midX = window.innerWidth / 2;
        for (var i = 0; i < videos.length; i++) {
          var r = videos[i].getBoundingClientRect();
          if (r.width < 2 || r.height < 2) {
            continue;
          }
          if (r.bottom < 0 || r.top > window.innerHeight) {
            continue;
          }
          var score =
            Math.abs(r.top + r.height / 2 - midY) * 2 +
            Math.abs(r.left + r.width / 2 - midX);
          if (score < bestScore) {
            bestScore = score;
            best = videos[i];
          }
        }
        return best;
      } catch (_err) {
        return null;
      }
    }

    /**
     * True when the reel has real decoded frames (not a black/unloaded stub).
     * readyState: 0=HAVE_NOTHING, 1=HAVE_METADATA, 2=HAVE_CURRENT_DATA,
     * 3=HAVE_FUTURE_DATA, 4=HAVE_ENOUGH_DATA
     */
    function isReelMediaReady(video) {
      try {
        if (!video) {
          return false;
        }
        if (video.error) {
          return false;
        }
        // Need dimensions + enough data to paint a frame.
        if (!(video.videoWidth > 0 && video.videoHeight > 0)) {
          return false;
        }
        if (video.readyState < 2) {
          return false;
        }
        // networkState 3 = NETWORK_NO_SOURCE (stuck / failed)
        if (video.networkState === 3) {
          return false;
        }
        return true;
      } catch (_err) {
        return false;
      }
    }

    async function waitForReelMediaReady(container, timeoutMs) {
      var start = performance.now();
      var lastLog = 0;
      while (performance.now() - start < timeoutMs) {
        var video = getReelVideo(container);
        if (isReelMediaReady(video)) {
          // Confirm it stays ready briefly (avoids flashing poster-only frames).
          await sleep(250);
          if (isReelMediaReady(getReelVideo(container))) {
            log(
              "REEL_MEDIA_READY readyState=" +
                (video && video.readyState) +
                " t=" +
                performance.now()
            );
            return true;
          }
        }
        if (performance.now() - lastLog > 3000) {
          lastLog = performance.now();
          log(
            "REEL_MEDIA_WAITING readyState=" +
              (video ? video.readyState : "none") +
              " networkState=" +
              (video ? video.networkState : "none") +
              " t=" +
              performance.now()
          );
        }
        await sleep(300);
      }
      warn("REEL_MEDIA_TIMEOUT — black/unloaded reel, skipping engage");
      return false;
    }

    async function closeCommentDialogIfOpen() {
      try {
        var dialogs = document.querySelectorAll('div[role="dialog"]');
        for (var i = 0; i < dialogs.length; i++) {
          var dialog = dialogs[i];
          // Prefer closing comment dialogs (those with composer), else any Close.
          var isComment = !!findCommentComposerInDialog(dialog);
          var closeBtn = findAriaControl(dialog, "Close");
          if (closeBtn && (isComment || dialogs.length === 1)) {
            humanPointerClick(closeBtn);
            await sleep(randBetween(400, 900));
            return;
          }
        }
        document.dispatchEvent(
          new KeyboardEvent("keydown", {
            key: "Escape",
            code: "Escape",
            keyCode: 27,
            which: 27,
            bubbles: true,
          })
        );
        await sleep(randBetween(300, 700));
      } catch (_err) {
        /* ignore */
      }
    }

    async function actionLike(container, reelId, index) {
      if (isReelAlreadyLiked(container)) {
        logBot(
          "ACTION_SKIPPED like reel_id=" +
            reelId +
            " index=" +
            index +
            " reason=already_liked"
        );
        var metaSkip = reelMetadataMap.get(reelId);
        if (metaSkip) {
          metaSkip.liked = true;
        }
        recordObserved(reelId, { liked: true });
        return true;
      }
      var btn = findEngageControl(container, "Like");
      if (!btn) {
        logBot(
          "ACTION_FAILED like reel_id=" +
            reelId +
            " index=" +
            index +
            " reason=button_not_found"
        );
        recordObserved(reelId, { liked: false });
        return false;
      }
      await sleep(randBetween(800, 2500));
      humanPointerClick(btn);
      await sleep(randBetween(400, 900));
      if (!isReelAlreadyLiked(container)) {
        // Retry once — UI lag / missed hit target.
        humanPointerClick(findEngageControl(container, "Like") || btn);
        await sleep(randBetween(500, 1000));
      }
      if (isReelAlreadyLiked(container)) {
        logBot("ACTION_LIKE reel_id=" + reelId + " index=" + index);
        var meta = reelMetadataMap.get(reelId);
        if (meta) {
          meta.liked = true;
        }
        recordObserved(reelId, { liked: true });
        return true;
      }
      logBot(
        "ACTION_FAILED like reel_id=" +
          reelId +
          " index=" +
          index +
          " reason=state_not_updated"
      );
      recordObserved(reelId, { liked: false });
      return false;
    }

    async function actionSave(container, reelId, index) {
      if (isReelAlreadySaved(container)) {
        logBot(
          "ACTION_SKIPPED save reel_id=" +
            reelId +
            " index=" +
            index +
            " reason=already_saved"
        );
        var metaSkipSave = reelMetadataMap.get(reelId);
        if (metaSkipSave) {
          metaSkipSave.saved = true;
        }
        recordObserved(reelId, { saved: true });
        return true;
      }
      var btn = findSaveControl(container);
      if (!btn) {
        await sleep(700);
        btn = findSaveControl(container);
      }
      if (!btn) {
        logBot(
          "ACTION_FAILED save reel_id=" +
            reelId +
            " index=" +
            index +
            " reason=button_not_found"
        );
        recordObserved(reelId, { saved: false });
        return false;
      }

      var before = snapshotSaveIcon(btn);
      await sleep(randBetween(400, 900));

      // Click the role=button wrapper (not only the SVG).
      humanPointerClick(btn);
      await sleep(300);
      await handleSaveCollectionDialog();

      var ok = false;
      var start = performance.now();
      while (performance.now() - start < 3500) {
        var live = findSaveControl(container) || btn;
        var after = snapshotSaveIcon(live);
        if (
          isSaveControlInSavedState(live) ||
          saveIconChanged(before, after) ||
          pageHasSavedToast()
        ) {
          ok = true;
          break;
        }
        await handleSaveCollectionDialog();
        await sleep(150);
      }

      // One retry only if still clearly outline / unchanged.
      if (!ok) {
        var again = findSaveControl(container) || btn;
        var mid = snapshotSaveIcon(again);
        if (mid.outline || !saveIconChanged(before, mid)) {
          humanPointerClick(again);
          await sleep(300);
          await handleSaveCollectionDialog();
          var start2 = performance.now();
          while (performance.now() - start2 < 2500) {
            var live2 = findSaveControl(container) || again;
            var after2 = snapshotSaveIcon(live2);
            if (
              isSaveControlInSavedState(live2) ||
              saveIconChanged(before, after2) ||
              pageHasSavedToast()
            ) {
              ok = true;
              break;
            }
            await sleep(150);
          }
        }
      }

      if (ok) {
        logBot("ACTION_SAVE reel_id=" + reelId + " index=" + index);
        var meta = reelMetadataMap.get(reelId);
        if (meta) {
          meta.saved = true;
        }
        recordObserved(reelId, { saved: true });
        return true;
      }

      logBot(
        "ACTION_FAILED save reel_id=" +
          reelId +
          " index=" +
          index +
          " reason=save_unconfirmed"
      );
      recordObserved(reelId, { saved: false });
      return false;
    }

    async function actionComment(container, reelId, index, commentText) {
      var text = commentText == null ? "" : String(commentText);
      if (!text) {
        logPlan(
          "PLAN_DEVIATION comment reel_id=" +
            reelId +
            " index=" +
            index +
            " reason=empty_comment_text"
        );
        recordObserved(reelId, { commented: false });
        return false;
      }

      var commentBtn = findEngageControl(container, "Comment");
      if (!commentBtn) {
        logBot(
          "ACTION_FAILED comment reel_id=" +
            reelId +
            " index=" +
            index +
            " reason=comment_button_not_found"
        );
        recordObserved(reelId, { commented: false });
        return false;
      }

      await sleep(randBetween(800, 2500));
      humanPointerClick(commentBtn);

      // Longer wait for slow networks.
      var opened = await waitForCommentComposer(10000);
      if (!opened) {
        // One retry click.
        humanPointerClick(findEngageControl(container, "Comment") || commentBtn);
        opened = await waitForCommentComposer(8000);
      }
      if (!opened) {
        logBot(
          "ACTION_FAILED comment reel_id=" +
            reelId +
            " index=" +
            index +
            " reason=composer_dialog_not_opened"
        );
        return false;
      }
      logBot(
        "COMMENT_BOX_OPENED reel_id=" +
          reelId +
          " index=" +
          index +
          " t=" +
          performance.now()
      );
      await sleep(randBetween(600, 1400));

      function liveInput() {
        var live = findLiveCommentComposer();
        return live ? live.input : null;
      }

      var typed = await typeIntoInputHuman(liveInput, text);
      if (!typed) {
        logBot(
          "ACTION_FAILED comment reel_id=" +
            reelId +
            " index=" +
            index +
            " reason=type_verify_failed"
        );
        await closeCommentDialogIfOpen();
        return false;
      }
      logBot(
        "COMMENT_TYPED reel_id=" +
          reelId +
          " index=" +
          index +
          " text=" +
          text
      );
      await sleep(randBetween(400, 900));

      // Focus + nudge before looking for Post — unfocused composers show text but no Post.
      await nudgeComposerForPost(liveInput);

      var live = findLiveCommentComposer();
      var dialog = live ? live.dialog : opened.dialog;
      var postBtn = await waitForPostButton(dialog, 5000);

      if (!postBtn) {
        // One more focus/nudge cycle; IG sometimes enables Post only after re-focus.
        await nudgeComposerForPost(liveInput);
        live = findLiveCommentComposer();
        dialog = live ? live.dialog : dialog;
        postBtn = await waitForPostButton(dialog, 3500);
      }

      if (postBtn) {
        // Ensure composer still focused first (some builds ignore Post while blurry).
        var prePost = liveInput();
        if (prePost) {
          await focusComposerInput(prePost, { click: false });
        }
        humanPointerClick(postBtn);
        logBot("COMMENT_POST_CLICKED reel_id=" + reelId + " index=" + index);
      } else {
        // Fallback Enter only works if the textbox is the active element.
        var input = liveInput();
        if (input) {
          await focusComposerInput(input, { click: true });
          input = liveInput() || input;
          placeComposerCaret(input);
          await sleep(randBetween(120, 280));
          var enterOpts = {
            key: "Enter",
            code: "Enter",
            keyCode: 13,
            which: 13,
            bubbles: true,
            cancelable: true,
          };
          input.dispatchEvent(new KeyboardEvent("keydown", enterOpts));
          input.dispatchEvent(new KeyboardEvent("keypress", enterOpts));
          input.dispatchEvent(new KeyboardEvent("keyup", enterOpts));
          logBot("COMMENT_POST_ENTER reel_id=" + reelId + " index=" + index);
        } else {
          logBot(
            "ACTION_FAILED comment reel_id=" +
              reelId +
              " index=" +
              index +
              " reason=post_control_not_found"
          );
          await closeCommentDialogIfOpen();
          return false;
        }
      }

      var submitted = await waitForCommentSubmitted(8000);
      if (!submitted) {
        logBot(
          "ACTION_FAILED comment reel_id=" +
            reelId +
            " index=" +
            index +
            " reason=submit_not_confirmed"
        );
        await closeCommentDialogIfOpen();
        return false;
      }

      await sleep(randBetween(400, 900));
      await closeCommentDialogIfOpen();

      logBot(
        "ACTION_COMMENT reel_id=" +
          reelId +
          " index=" +
          index +
          " text=" +
          text
      );
      var meta = reelMetadataMap.get(reelId);
      if (meta) {
        meta.commented = true;
      }
      recordObserved(reelId, { commented: true, comment_text: text });
      return true;
    }

    /**
     * Execute server plan: action (like|save|null) and comment are independent.
     */
    async function engageOnReel(job, decision) {
      var container = job.containerEl;
      if (!container || !container.isConnected) {
        // Fallback: most recently accepted connected container
        for (var i = observedContainers.length - 1; i >= 0; i--) {
          if (observedContainers[i] && observedContainers[i].isConnected) {
            container = observedContainers[i];
            break;
          }
        }
      }
      if (!container) {
        logPlan(
          "PLAN_DEVIATION reel_id=" +
            job.reelId +
            " reason=no_container"
        );
        return false;
      }

      var index = job.index;
      var ok = true;
      var action = decision && decision.action ? String(decision.action) : null;
      var comment =
        decision && decision.comment != null && decision.comment !== ""
          ? String(decision.comment)
          : null;

      if (action && action !== "like" && action !== "save") {
        logPlan(
          "PLAN_DEVIATION reel_id=" +
            job.reelId +
            " reason=unknown_action value=" +
            action
        );
        ok = false;
        action = null;
      }

      if (action === "like") {
        logBot(
          "PLAN_ACTION like reel_id=" + job.reelId + " index=" + index
        );
        var likeOk = await actionLike(container, job.reelId, index);
        if (!likeOk) {
          logPlan(
            "PLAN_DEVIATION reel_id=" +
              job.reelId +
              " reason=like_failed"
          );
        }
        ok = likeOk && ok;
        await sleep(randBetween(1200, 3500));
      } else if (action === "save") {
        logBot(
          "PLAN_ACTION save reel_id=" + job.reelId + " index=" + index
        );
        var saveOk = await actionSave(container, job.reelId, index);
        if (!saveOk) {
          logPlan(
            "PLAN_DEVIATION reel_id=" +
              job.reelId +
              " reason=save_failed"
          );
        }
        ok = saveOk && ok;
        await sleep(randBetween(1200, 3500));
      }

      if (comment) {
        logBot(
          "PLAN_ACTION comment reel_id=" +
            job.reelId +
            " index=" +
            index +
            " text=" +
            comment
        );
        var commentOk = await actionComment(
          container,
          job.reelId,
          index,
          comment
        );
        if (!commentOk) {
          logPlan(
            "PLAN_DEVIATION reel_id=" +
              job.reelId +
              " reason=comment_failed"
          );
        }
        ok = commentOk && ok;
        await sleep(randBetween(1200, 3500));
      }

      if (!action && !comment) {
        logBot(
          "PLAN_ACTION none reel_id=" +
            job.reelId +
            " index=" +
            index +
            " (watch-only)"
        );
      }

      return ok;
    }

    function dispatchArrowDown(target) {
      try {
        if (!target) {
          return;
        }
        // Include legacy keyCode/which — Instagram/React handlers still check them.
        var opts = {
          key: "ArrowDown",
          code: "ArrowDown",
          keyCode: 40,
          which: 40,
          bubbles: true,
          cancelable: true,
          view: window,
        };
        target.dispatchEvent(new KeyboardEvent("keydown", opts));
        target.dispatchEvent(new KeyboardEvent("keypress", opts));
        target.dispatchEvent(new KeyboardEvent("keyup", opts));
      } catch (_err) {
        /* ignore */
      }
    }

    function focusReelForKeyboard(container) {
      try {
        var hit = null;
        if (container && container.isConnected) {
          hit =
            container.querySelector("video") ||
            container.querySelector('[role="button"]') ||
            container;
        }
        if (!hit) {
          hit = document.querySelector("video") || document.body;
        }
        // Click to give Instagram's reel surface keyboard focus (critical for ArrowDown).
        humanPointerClick(hit);
        if (typeof hit.focus === "function") {
          hit.focus({ preventScroll: true });
        }
        return hit;
      } catch (_err) {
        return document.body;
      }
    }

    async function pressArrowDownAdvance() {
      var container =
        (currentAutopilotReel && currentAutopilotReel.containerEl) || null;
      var focused = focusReelForKeyboard(container);
      await sleep(randBetween(150, 320));

      // Single ArrowDown — double taps skip reels in the feed.
      var targets = [
        focused,
        document.activeElement,
        window,
        document,
        document.documentElement,
        document.body,
      ];
      for (var i = 0; i < targets.length; i++) {
        dispatchArrowDown(targets[i]);
      }
      await sleep(randBetween(500, 900));
    }

    async function scrollToNextReelHuman() {
      // If queue already has the next reel(s), don't advance again.
      if (autopilotQueue.length > 0) {
        logBot(
          "AUTOPILOT_SCROLL skipped — queue already has " +
            autopilotQueue.length +
            " reel(s)"
        );
        return;
      }

      logBot("AUTOPILOT_SCROLL method=ArrowDown t=" + performance.now());
      await closeCommentDialogIfOpen();
      await pressArrowDownAdvance();
      await sleep(randBetween(600, 1200));
    }

    async function autopilotLoop() {
      if (!AUTOPILOT_ENABLED) {
        logBot("AUTOPILOT disabled");
        return;
      }
      if (autopilotBusy) {
        return;
      }
      autopilotBusy = true;
      logBot(
        "AUTOPILOT_START api=" +
          (getApiBaseUrl() || "MISSING") +
          " t=" +
          performance.now()
      );

      var previousId = null;

      while (AUTOPILOT_ENABLED) {
        try {
          var job = await waitForNextAutopilotReel(previousId);
          if (!job) {
            await sleep(1000);
            continue;
          }

          logBot(
            "AUTOPILOT_REEL reel_id=" +
              job.reelId +
              " index=" +
              job.index +
              " queue_left=" +
              autopilotQueue.length +
              " t=" +
              performance.now()
          );

          // Do not engage/dwell-count on a black / unloaded reel.
          var mediaReady = await waitForReelMediaReady(
            job.containerEl,
            20000
          );
          if (!mediaReady) {
            autopilotFailStreak += 1;
            recordObserved(job.reelId, {
              skipped_engage: true,
              skip_reason: "media_not_ready",
              liked: false,
              saved: false,
              commented: false,
            });
            logReelResult(job, reelDecisionMap.get(job.reelId) || null, {
              skip_reason: "media_not_ready",
              liked: false,
              saved: false,
              commented: false,
            });
            // Not watched — do not ingest; free memory.
            finishAutopilotReel(job.reelId, false);
            previousId = job.reelId;
            currentAutopilotReel = null;
            await scrollToNextReelHuman();
            continue;
          }

          var decision = await waitForReelDecision(
            job.reelId,
            DECISION_WAIT_TIMEOUT_MS
          );
          var watchOnly = false;
          if (!decision) {
            decision = {
              action: null,
              comment: null,
              duration: DEFAULT_WATCH_DURATION_S,
              receivedAt: performance.now(),
            };
            watchOnly = true;
          }
          // duration = watch time only. Comment ⇒ at least 15s watch (human reads reel first).
          var durationS = normalizePlanDurationS(decision);
          recordExpected(job.reelId, decision);
          if (watchOnly) {
            recordObserved(job.reelId, {
              skipped_engage: true,
              skip_reason: "decision_timeout",
            });
          }

          var watchMs = Math.round(durationS * 1000);
          var watchStart = performance.now();
          await sleepExact(watchMs);
          var watchElapsed = performance.now() - watchStart;

          // Re-check after watch — connection can die mid-slot.
          if (!isReelMediaReady(getReelVideo(job.containerEl))) {
            recordObserved(job.reelId, {
              skipped_engage: true,
              skip_reason: "media_lost_after_dwell",
              dwell_planned_s: durationS,
              watch_s: watchElapsed / 1000,
              engage_s: 0,
            });
            var obsLost = (reelRunLog.get(job.reelId) || {}).observed || {};
            logReelResult(job, decision, obsLost);
            // Watch completed — ingest then free memory.
            finishAutopilotReel(job.reelId, true);
            previousId = job.reelId;
            currentAutopilotReel = null;
            await scrollToNextReelHuman();
            continue;
          }

          // Engage after watch (like a real user) — does not count against watch_s.
          var engageOk = true;
          var engageStart = performance.now();
          if (watchOnly) {
            engageOk = true;
          } else {
            engageOk = await engageOnReel(job, decision);
          }
          var engageElapsed = performance.now() - engageStart;

          var metaAfter = reelMetadataMap.get(job.reelId) || {};
          recordObserved(job.reelId, {
            liked: !!metaAfter.liked,
            saved: !!metaAfter.saved,
            commented: !!metaAfter.commented,
            engage_ok: !!engageOk,
            dwell_planned_s: durationS,
            watch_s: watchElapsed / 1000,
            engage_s: engageElapsed / 1000,
          });

          var obsFinal = (reelRunLog.get(job.reelId) || {}).observed || {};
          logReelResult(job, decision, obsFinal);

          if (engageOk) {
            autopilotFailStreak = 0;
          } else {
            autopilotFailStreak += 1;
            if (autopilotFailStreak >= 3) {
              await sleep(randBetween(30000, 60000));
              autopilotFailStreak = 0;
            }
          }

          // Strict: Supabase only gets reels the bot actually watched.
          finishAutopilotReel(job.reelId, true);
          await sleep(randBetween(800, 1600));
          previousId = job.reelId;
          currentAutopilotReel = null;

          // Only scroll if the queue is empty (don't skip ahead of queued reels).
          if (autopilotQueue.length === 0) {
            await scrollToNextReelHuman();

            var waitStart = performance.now();
            while (
              performance.now() - waitStart < 8000 &&
              autopilotQueue.length === 0
            ) {
              await sleep(250);
            }
            if (autopilotQueue.length === 0) {
              warn(
                "AUTOPILOT_STALLED retrying ArrowDown t=" + performance.now()
              );
              await pressArrowDownAdvance();
              await sleep(randBetween(1000, 1800));
            }
          } else {
            logBot(
              "AUTOPILOT_CATCHUP processing queued reel(s) count=" +
                autopilotQueue.length
            );
          }
        } catch (loopErr) {
          warn("AUTOPILOT loop error: " + (loopErr && loopErr.message));
          await sleep(3000);
        }
      }
    }

    function startAutopilot() {
      try {
        if (!AUTOPILOT_ENABLED) {
          return;
        }
        // Defer so initial embedded metadata + first viewport can settle.
        setTimeout(function () {
          autopilotLoop().catch(function (err) {
            warn("AUTOPILOT crashed: " + (err && err.message));
            autopilotBusy = false;
          });
        }, 2500);
      } catch (_err) {
        warn("AUTOPILOT failed to start");
      }
    }

    function startDomObservers() {
      try {
        // Prefer metadata from HTML Relay prefetch before viewport matching.
        scanEmbeddedRelayScripts(document);
        scanForVideos(document);

        var mutationObserver = new MutationObserver(onMutations);
        var root = document.documentElement || document;
        mutationObserver.observe(root, {
          childList: true,
          subtree: true,
        });

        // Re-scan periodically for late scripts / zero-size containers.
        setInterval(function () {
          try {
            scanEmbeddedRelayScripts(document);
            scanForVideos(document);
            flushPendingViewportMatches();
          } catch (_intervalErr) {
            /* ignore */
          }
        }, 1000);
      } catch (_domErr) {
        warn("WARN failed to start DOM observers");
      }
    }

    // Scan immediately — at document_start some JSON scripts may already exist.
    try {
      scanEmbeddedRelayScripts(document);
    } catch (_earlyScanErr) {
      /* ignore */
    }

    if (document.documentElement) {
      startDomObservers();
    } else {
      document.addEventListener("DOMContentLoaded", startDomObservers, {
        once: true,
      });
    }

    startAutopilot();

    // Silent install when QUIET_* flags are on.
  } catch (_topErr) {
    try {
      console.warn("[REEL-TIMING] WARN top-level init failed", _topErr);
    } catch (_ignore) {
      /* last resort */
    }
  }
})();
