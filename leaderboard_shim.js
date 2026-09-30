/*
 * PolyTrack community leaderboard shim (client-side).
 *
 * Intercepts XMLHttpRequest to the game's "v6" endpoints and serves them
 * straight from Supabase (PostgREST) using the public anon key, so the game
 * works from any static host (e.g. GitHub Pages) with no backend.
 *
 * Auth model (requires RLS on Supabase):
 *   lb_users / lb_entries: SELECT for everyone, INSERT/UPDATE for everyone
 *   (writes are rate-limited by Postgres constraints; service key stays secret).
 */
(function () {
  "use strict";

  var SUPABASE_URL = window.POLYTRACK_SUPABASE_URL || "";
  var ANON_KEY = window.POLYTRACK_SUPABASE_ANON_KEY || "";
  var API_PREFIX = "/api/v6/";

  if (!SUPABASE_URL || !ANON_KEY) return; // not configured; let requests fail normally

  var nativeOpen = XMLHttpRequest.prototype.open;
  var nativeSend = XMLHttpRequest.prototype.send;
  var nativeSetHeader = XMLHttpRequest.prototype.setRequestHeader;

  function headers(extra) {
    var h = {
      apikey: ANON_KEY,
      Authorization: "Bearer " + ANON_KEY,
      "Content-Type": "application/json",
    };
    if (extra) for (var k in extra) h[k] = extra[k];
    return h;
  }

  function isApiUrl(method, url) {
    if (typeof url !== "string") return false;
    var abs = url.startsWith("http") ? url : location.origin + url;
    return abs.indexOf(API_PREFIX) !== -1 && (method === "GET" || method === "POST");
  }

  function parseBody(body) {
    if (!body) return new URLSearchParams();
    if (typeof body === "string") return new URLSearchParams(body);
    return new URLSearchParams();
  }

  function sba(path) {
    return SUPABASE_URL.replace(/\/$/, "") + "/rest/v1/" + path;
  }

  function getJson(url, extra) {
    return fetch(url, { headers: headers(extra) }).then(function (r) {
      if (!r.ok) throw new Error("supabase " + r.status);
      return r.json();
    });
  }

  function userHash(token) {
    // sha256 hex, first 32 chars - matches server implementation
    return crypto.subtle.digest("SHA-256", new TextEncoder().encode(token)).then(function (buf) {
      return Array.from(new Uint8Array(buf))
        .map(function (b) { return b.toString(16).padStart(2, "0"); })
        .join("")
        .slice(0, 32);
    });
  }

  function isoNow() {
    return new Date().toISOString().replace(/\.\d+Z$/, "Z");
  }

  function fetchEntries(trackId) {
    var url =
      sba("lb_entries?track_id=eq." + encodeURIComponent(trackId)) +
      "&order=frames.asc,id.asc&limit=1000&select=id,user_id,nickname,frames,time,car_style,country_code,recording";
    return getJson(url).then(function (rows) {
      return (rows || []).map(function (r) {
        return {
          id: Number(r.id),
          userId: r.user_id,
          nickname: r.nickname,
          frames: Number(r.frames),
          time: r.time,
          carStyle: r.car_style,
          countryCode: r.country_code == null ? null : r.country_code,
          recording: r.recording || "",
        };
      });
    });
  }

  function entryJson(e) {
    return {
      id: e.id,
      userId: e.userId,
      nickname: e.nickname,
      frames: e.frames,
      time: e.time,
      carStyle: e.carStyle,
      verifiedState: 1,
      countryCode: e.countryCode,
    };
  }

  function handle(path, query, body) {
    var q = new URLSearchParams(query || "");
    var endpoint = path.split("/").pop();

    if (q.get("__post") === "1") {
      var form = body;
      // ---- POST /leaderboard ----
      if (endpoint === "leaderboard") {
        var token = form.get("userToken") || "";
        var nickname = form.get("nickname") || "";
        var country = form.get("countryCode");
        var carStyle = form.get("carStyle") || "";
        var trackId = form.get("trackId") || "";
        var frames = parseInt(form.get("frames") || "0", 10) || 0;
        var recording = form.get("recording") || "";
        if (!/^[A-Za-z0-9]{1,128}$/.test(token) || !nickname || nickname.length > 50) {
          return Promise.reject(new Error("bad request"));
        }
        if (!/^[A-Za-z0-9_]{1,80}$/.test(trackId)) return Promise.reject(new Error("bad request"));
        if (frames <= 0 || !recording || recording.length >= 60000) {
          return Promise.reject(new Error("bad request"));
        }
        return userHash(token).then(function (userId) {
          var upsertUser = fetch(sba("lb_users"), {
            method: "POST",
            headers: headers({ Prefer: "resolution=merge-duplicates" }),
            body: JSON.stringify({
              user_id: userId,
              nickname: nickname,
              country_code: country || null,
              car_style: carStyle,
            }),
          }).then(function (r) { if (!r.ok) throw new Error("user upsert failed"); });

          return upsertUser
            .then(function () { return fetchEntries(trackId); })
            .then(function (entries) {
              var idx = -1;
              for (var i = 0; i < entries.length; i++) {
                if (entries[i].userId === userId) { idx = i; break; }
              }
              var prevPos = idx >= 0 ? idx + 1 : 0;
              var existing = idx >= 0 ? entries[idx] : null;
              var row = {
                track_id: trackId,
                user_id: userId,
                nickname: nickname,
                country_code: country || null,
                car_style: carStyle,
                frames: frames,
                time: isoNow(),
                recording: recording,
              };
              var p;
              if (existing) {
                if (frames < existing.frames) {
                  p = fetch(sba("lb_entries?id=eq." + existing.id), {
                    method: "PATCH",
                    headers: headers(),
                    body: JSON.stringify(row),
                  }).then(function (r) { if (!r.ok) throw new Error("update failed"); });
                } else {
                  p = Promise.resolve();
                }
              } else {
                p = fetch(sba("lb_entries"), {
                  method: "POST",
                  headers: headers(),
                  body: JSON.stringify(row),
                }).then(function (r) { if (!r.ok) throw new Error("insert failed"); });
              }
              return p.then(function () {
                return fetchEntries(trackId).then(function (after) {
                  var entryId = existing ? existing.id : -1;
                  var newPos = -1;
                  for (var j = 0; j < after.length; j++) {
                    if (existing ? after[j].id === entryId : after[j].userId === userId) {
                      entryId = after[j].id;
                      newPos = j + 1;
                      break;
                    }
                  }
                  if (newPos <= 0) newPos = after.length;
                  return {
                    uploadId: entryId,
                    previousPosition: prevPos || newPos,
                    newPosition: newPos,
                  };
                });
              });
            });
        });
      }

      // ---- POST /user ----
      if (endpoint === "user") {
        var t = form.get("userToken") || "";
        var nick = form.get("nickname") || "";
        var cc = form.get("countryCode");
        var cs = form.get("carStyle") || "";
        if (!/^[A-Za-z0-9]{1,128}$/.test(t) || !nick || nick.length > 50) {
          return Promise.reject(new Error("bad request"));
        }
        return userHash(t).then(function (userId) {
          return fetch(sba("lb_users"), {
            method: "POST",
            headers: headers({ Prefer: "resolution=merge-duplicates" }),
            body: JSON.stringify({
              user_id: userId,
              nickname: nick,
              country_code: cc || null,
              car_style: cs,
            }),
          }).then(function (r) { if (!r.ok) throw new Error("upsert failed"); return ""; });
        });
      }

      return Promise.reject(new Error("unknown endpoint"));
    }

    // ---- GET endpoints ----
    if (endpoint === "leaderboard") {
      var trackId = q.get("trackId") || "";
      var skip = parseInt(q.get("skip") || "0", 10) || 0;
      var amount = Math.min(parseInt(q.get("amount") || "20", 10) || 20, 100);
      var hash = q.get("userTokenHash") || "";
      return fetchEntries(trackId).then(function (entries) {
        var userEntry = null;
        if (hash) {
          for (var i = 0; i < entries.length; i++) {
            if (entries[i].userId === hash) {
              userEntry = { position: i + 1, frames: entries[i].frames, id: entries[i].id };
              break;
            }
          }
        }
        return {
          total: entries.length,
          entries: entries.slice(skip, skip + amount).map(entryJson),
          userEntry: userEntry,
        };
      });
    }

    if (endpoint === "leaderboardUserEntry") {
      var trackId2 = q.get("trackId") || "";
      var hash2 = q.get("userTokenHash") || "";
      return fetchEntries(trackId2).then(function (entries) {
        for (var i = 0; i < entries.length; i++) {
          if (entries[i].userId === hash2) {
            return { position: i + 1, frames: entries[i].frames, id: entries[i].id };
          }
        }
        return null;
      });
    }

    if (endpoint === "recordings") {
      var ids = (q.get("ids") || "").split(",").filter(Boolean);
      return Promise.all(
        ids.map(function (raw) {
          var id = parseInt(raw, 10);
          if (!Number.isSafeInteger(id)) return Promise.resolve(null);
          return getJson(
            sba("lb_entries?id=eq." + id + "&limit=1&select=recording,frames,car_style")
          ).then(function (rows) {
            if (!rows || rows.length === 0) return null;
            return {
              recording: rows[0].recording,
              verifiedState: 1,
              frames: Number(rows[0].frames),
              carStyle: rows[0].car_style,
            };
          }).catch(function () { return null; });
        })
      );
    }

    if (endpoint === "user") {
      var tok = q.get("userToken") || "";
      return userHash(tok).then(function (userId) {
        return getJson(
          sba("lb_users?user_id=eq." + userId + "&limit=1&select=nickname,country_code,car_style")
        ).then(function (rows) {
          if (!rows || rows.length === 0) return null;
          return {
            nickname: rows[0].nickname,
            countryCode: rows[0].country_code == null ? null : rows[0].country_code,
            carStyle: rows[0].car_style || "",
            isVerifier: false,
          };
        }).catch(function () { return null; });
      });
    }

    if (endpoint === "iceServers") return Promise.resolve([]);
    if (endpoint === "trackOfTheWeek") return Promise.resolve({ serverTime: isoNow(), current: null });

    return Promise.reject(new Error("unknown endpoint"));
  }

  // Patch XHR so the game's unmodified code paths work.
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__ptIntercept = isApiUrl(method, url);
    this.__ptMethod = method;
    this.__ptUrl = url;
    this.__ptHeaders = {};
    return nativeOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    if (this.__ptIntercept) { this.__ptHeaders[k] = v; return; }
    return nativeSetHeader.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    if (!this.__ptIntercept) return nativeSend.apply(this, arguments);

    var xhr = this;
    var abs = this.__ptUrl.startsWith("http")
      ? this.__ptUrl
      : location.origin + this.__ptUrl;
    var path = abs.substring(abs.indexOf(API_PREFIX) + API_PREFIX.length);
    var qIndex = path.indexOf("?");
    var pathOnly = qIndex >= 0 ? path.substring(0, qIndex) : path;
    var query = qIndex >= 0 ? path.substring(qIndex + 1) : "";
    var form = parseBody(body);

    handle(pathOnly, query, form).then(
      function (result) {
        Object.defineProperty(xhr, "status", { value: 200 });
        Object.defineProperty(xhr, "readyState", { value: 4 });
        Object.defineProperty(xhr, "responseText", { value: JSON.stringify(result) });
        Object.defineProperty(xhr, "response", { value: JSON.stringify(result) });
        if (typeof xhr.onreadystatechange === "function") xhr.onreadystatechange();
        if (typeof xhr.onload === "function") xhr.onload();
        xhr.dispatchEvent(new Event("readystatechange"));
        xhr.dispatchEvent(new ProgressEvent("load"));
      },
      function (err) {
        Object.defineProperty(xhr, "status", { value: 0 });
        Object.defineProperty(xhr, "readyState", { value: 4 });
        if (typeof xhr.onreadystatechange === "function") xhr.onreadystatechange();
        xhr.dispatchEvent(new Event("readystatechange"));
        xhr.dispatchEvent(new ProgressEvent("error"));
      }
    );
  };
})();
