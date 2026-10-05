/* Pocketful - shared client runtime: money, decimal parsing, API, rendering, refresh. */
(function () {
  "use strict";

  function meta(name) {
    var el = document.querySelector('meta[name="' + name + '"]');
    return el ? (el.getAttribute("content") || "") : "";
  }

  var config = {
    currency: meta("app-currency") || "EUR",
    minorUnits: parseInt(meta("app-minor-units") || "2", 10),
    token: meta("app-token"),
    userId: meta("app-user-id"),
  };
  if (!Number.isFinite(config.minorUnits)) config.minorUnits = 2;

  var ERROR_MESSAGES = {
    insufficient_funds: "Insufficient available funds.",
    self_payment: "You cannot pay your own handle.",
    self_request: "You cannot request money from yourself.",
    not_found: "We could not find that handle or item.",
    forbidden: "You do not have permission to do that.",
    unauthenticated: "Your session has expired. Please sign in again.",
    email_taken: "That email is already registered.",
    handle_taken: "That handle is already taken.",
    request_not_pending: "That request is no longer pending.",
    authorization_not_open: "This authorization is no longer open.",
    authorization_expired: "This authorization has expired.",
    capture_exceeds_authorization: "The capture is more than the remaining amount.",
    idempotency_key_reuse: "This form changed while it was being sent. Try again.",
    validation_failed: "Please check the values and try again.",
    malformed_request: "That request could not be understood.",
    missing_idempotency_key: "Something went wrong. Please try again.",
  };

  function money(minor) {
    var n = Number(minor);
    if (!Number.isFinite(n)) n = 0;
    if (config.minorUnits === 0) return String(Math.trunc(n)) + " " + config.currency;
    var factor = Math.pow(10, config.minorUnits);
    return (n / factor).toFixed(config.minorUnits) + " " + config.currency;
  }

  function decimalString(minor) {
    var n = Number(minor);
    if (!Number.isFinite(n)) n = 0;
    if (config.minorUnits === 0) return String(Math.trunc(n));
    return (n / Math.pow(10, config.minorUnits)).toFixed(config.minorUnits);
  }

  function parseDecimal(raw) {
    var text = String(raw == null ? "" : raw).trim();
    if (!text) return { ok: false, error: "Enter an amount." };
    if (!/^\d+(\.\d+)?$/.test(text)) return { ok: false, error: "Enter a number, for example 15.00." };
    var parts = text.split(".");
    var whole = parts[0];
    var frac = parts[1] || "";
    if (frac.length > config.minorUnits) {
      return {
        ok: false,
        error: config.minorUnits === 0
          ? "This currency does not use decimal places."
          : "Use at most " + config.minorUnits + " decimal places.",
      };
    }
    var minor = parseInt(whole, 10) * Math.pow(10, config.minorUnits);
    if (config.minorUnits > 0) minor += parseInt((frac + "0".repeat(config.minorUnits)).slice(0, config.minorUnits) || "0", 10);
    if (!Number.isFinite(minor) || minor < 1) return { ok: false, error: "Enter an amount of at least 1." };
    return { ok: true, minor: minor };
  }

  function errorMessage(res) {
    var data = res && res.data;
    var code = data && data.error && data.error.code;
    if (code && ERROR_MESSAGES[code]) return ERROR_MESSAGES[code];
    if (data && data.error && data.error.message) return data.error.message;
    if (!res || res.networkError) return "We could not reach the server. Please try again.";
    return "Something went wrong (HTTP " + (res.status || "?") + ").";
  }

  function newKey() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return "k_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2);
  }

  function api(path, options) {
    options = options || {};
    var headers = { Accept: options.accept || "application/json" };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    if (config.token) headers["Authorization"] = "Bearer " + config.token;
    if (options.key) headers["Idempotency-Key"] = options.key;
    return fetch(path, {
      method: options.method || "GET",
      headers: headers,
      credentials: "same-origin",
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    }).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
        return { ok: res.ok, status: res.status, data: data };
      });
    }).catch(function (err) {
      return { networkError: true, error: err };
    });
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function q(testid) { return document.querySelector('[data-testid="' + testid + '"]'); }

  function atHandle(handle) { return "@" + String(handle == null ? "" : handle); }

  function timeText(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso == null ? "" : iso);
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function setAlert(opts) {
    var existing = q(opts.testid);
    if (existing) existing.parentNode.removeChild(existing);
    if (!opts.message) return;
    var node = document.createElement(opts.tag || "p");
    node.setAttribute("data-testid", opts.testid);
    node.className = "alert alert--" + (opts.variant || "error");
    node.textContent = opts.message;
    var host = opts.host || document.querySelector("main") || document.body;
    host.appendChild(node);
  }

  function clearAlert(testid) {
    var existing = q(testid);
    if (existing) existing.parentNode.removeChild(existing);
  }

  /* ---------- wallet ---------- */

  var seq = { wallet: 0, feed: 0, requests: 0, authorizations: 0 };

  function setWalletNumber(testid, minor) {
    var el = q(testid);
    if (!el) return;
    el.textContent = money(minor);
    el.setAttribute("data-amount", String(minor));
  }

  function applyWallet(me) {
    if (!me) return;
    var total = me.total != null ? me.total : me.balance;
    var available = me.available != null ? me.available : total;
    var held = me.held != null ? me.held : 0;
    setWalletNumber("wallet-balance", total);
    setWalletNumber("wallet-available", available);
    var heldEl = q("wallet-held");
    if (held > 0) {
      if (!heldEl) {
        var anchor = document.getElementById("wallet-balance-item");
        if (anchor) anchor.insertAdjacentHTML("afterend",
          '<div class="walletbar__item" id="wallet-held-item">' +
          '<span class="walletbar__label">Held</span>' +
          '<span class="walletbar__amount walletbar__amount--secondary" data-testid="wallet-held"></span></div>');
        heldEl = q("wallet-held");
      }
      if (heldEl) { heldEl.textContent = money(held); heldEl.setAttribute("data-amount", String(held)); }
    } else {
      var item = document.getElementById("wallet-held-item");
      if (item) item.parentNode.removeChild(item);
      else if (heldEl) heldEl.parentNode.removeChild(heldEl);
    }
  }

  function refreshWallet() {
    var n = ++seq.wallet;
    return api("/me").then(function (res) {
      if (n !== seq.wallet) return;            // a later refresh already started
      if (res.ok && res.data) applyWallet(res.data);
    });
  }

  /* ---------- activity feed ---------- */

  function activityItem(payment) {
    var id = payment.payment_id;
    var visibility = payment.visibility || "public";
    var note = payment.note == null ? "" : payment.note;
    return '<li class="listitem activity-item" data-testid="activity-item-' + escapeHtml(id) + '" data-visibility="' + escapeHtml(visibility) + '">' +
      '<div class="listitem__main">' +
      '<div class="listitem__title">' +
      '<span data-testid="activity-parties-' + escapeHtml(id) + '">' + escapeHtml(atHandle(payment.from_handle)) + " \u2192 " + escapeHtml(atHandle(payment.to_handle)) + "</span>" +
      '<span class="badge badge--' + escapeHtml(visibility) + '">' + escapeHtml(visibility) + "</span></div>" +
      '<div class="activity-note' + (note ? "" : " activity-note--empty") + '" data-testid="activity-note-' + escapeHtml(id) + '">' + escapeHtml(note) + "</div>" +
      '<div class="meta">' + escapeHtml(timeText(payment.created_at)) + "</div></div>" +
      '<div class="listitem__side"><span class="money money--big" data-testid="activity-amount-' + escapeHtml(id) + '">' + escapeHtml(money(payment.amount)) + "</span></div></li>";
  }

  function renderActivity(payments) {
    var list = q("activity-list");
    var empty = q("empty-activity");
    var items = (payments || []).slice().sort(function (a, b) {
      return String(b.created_at || "").localeCompare(String(a.created_at || ""));
    });
    if (list) {
      list.innerHTML = items.map(activityItem).join("");
      list.hidden = items.length === 0;
    }
    if (empty) empty.hidden = items.length !== 0;
  }

  function refreshFeed() {
    var n = ++seq.feed;
    return api("/activity").then(function (res) {
      if (n !== seq.feed || !res.ok || !res.data) return;
      renderActivity(res.data.payments);
    });
  }

  /* ---------- requests ---------- */

  function requestItem(item, incoming) {
    var id = item.request_id;
    var status = item.status || "pending";
    var note = item.note == null ? "" : item.note;
    var actions = "";
    if (status === "pending" && incoming) {
      actions = '<button class="btn btn--primary btn--sm" type="button" data-action="pay-request" data-id="' + escapeHtml(id) + '" data-testid="request-pay-' + escapeHtml(id) + '">Pay</button>' +
        '<button class="btn btn--ghost btn--sm" type="button" data-action="decline-request" data-id="' + escapeHtml(id) + '" data-testid="request-decline-' + escapeHtml(id) + '">Decline</button>';
    } else if (status === "pending" && !incoming) {
      actions = '<button class="btn btn--ghost btn--sm" type="button" data-action="cancel-request" data-id="' + escapeHtml(id) + '" data-testid="request-cancel-' + escapeHtml(id) + '">Cancel</button>';
    }
    return '<li class="listitem" data-testid="request-item-' + escapeHtml(id) + '" data-status="' + escapeHtml(status) + '">' +
      '<div class="listitem__main"><div class="listitem__title">' +
      "<span>" + escapeHtml(atHandle(item.requester_handle)) + " \u2192 " + escapeHtml(atHandle(item.payer_handle)) + "</span>" +
      '<span class="badge badge--' + escapeHtml(status) + '">' + escapeHtml(status) + "</span></div>" +
      '<div class="listitem__sub"' + (note ? "" : ' style="display:none"') + ">" + escapeHtml(note) + "</div>" +
      '<div class="meta">' + escapeHtml(timeText(item.created_at)) + "</div></div>" +
      '<div class="listitem__side"><span class="money" data-testid="request-amount-' + escapeHtml(id) + '">' + escapeHtml(money(item.amount)) + "</span>" + actions + "</div></li>";
  }

  function renderRequests(requests) {
    var incoming = [];
    var outgoing = [];
    (requests || []).forEach(function (item) {
      var mine = item.payer_id === config.userId || item.requester_id === config.userId;
      if (!mine) return;
      if (item.payer_id === config.userId) incoming.push(item); else outgoing.push(item);
    });
    var incList = q("incoming-list");
    var outList = q("outgoing-list");
    if (incList) incList.innerHTML = incoming.map(function (i) { return requestItem(i, true); }).join("");
    if (outList) outList.innerHTML = outgoing.map(function (i) { return requestItem(i, false); }).join("");
    var incEmpty = q("incoming-empty");
    var outEmpty = q("outgoing-empty");
    if (incEmpty) incEmpty.hidden = incoming.length !== 0;
    if (outEmpty) outEmpty.hidden = outgoing.length !== 0;
    var empty = q("empty-requests");
    if (empty) empty.hidden = (incoming.length + outgoing.length) !== 0;
  }

  function refreshRequests() {
    var n = ++seq.requests;
    return api("/requests").then(function (res) {
      if (n !== seq.requests || !res.ok || !res.data) return;
      renderRequests(res.data.requests);
    });
  }

  /* ---------- authorizations ---------- */

  function authorizationItem(item) {
    var id = item.authorization_id;
    var status = item.status || "open";
    var visibility = item.visibility || "public";
    var incoming = item.to_user_id === config.userId;
    var outgoing = item.from_user_id === config.userId;
    var note = item.note == null ? "" : item.note;
    var captured = "";
    if (status === "captured") {
      captured = '<div class="listitem__sub">Captured <span class="money" data-testid="authorization-captured-' + escapeHtml(id) + '">' + escapeHtml(money(item.captured_amount || 0)) + "</span></div>";
    }
    var actions = "";
    if (incoming && status === "open") {
      actions = '<div class="capture">' +
        '<input class="input input--amount" type="text" inputmode="decimal" aria-label="Capture amount" data-testid="authorization-capture-amount-' + escapeHtml(id) + '" value="' + escapeHtml(decimalString(item.remaining_amount != null ? item.remaining_amount : item.amount)) + '">' +
        '<button class="btn btn--primary btn--sm" type="button" data-action="capture-authorization" data-id="' + escapeHtml(id) + '" data-testid="authorization-capture-' + escapeHtml(id) + '">Capture</button></div>';
    } else if (outgoing && status === "open") {
      actions = '<button class="btn btn--danger btn--sm" type="button" data-action="void-authorization" data-id="' + escapeHtml(id) + '" data-testid="authorization-void-' + escapeHtml(id) + '">Void</button>';
    }
    return '<li class="listitem listitem--guttered" data-testid="authorization-item-' + escapeHtml(id) + '" data-status="' + escapeHtml(status) + '">' +
      '<div class="listitem__main"><div class="listitem__title">' +
      "<span>" + escapeHtml(atHandle(item.from_handle)) + " \u2192 " + escapeHtml(atHandle(item.to_handle)) + "</span>" +
      '<span class="badge badge--' + escapeHtml(status) + '">' + escapeHtml(status) + "</span>" +
      '<span class="badge badge--' + escapeHtml(visibility) + '">' + escapeHtml(visibility) + "</span></div>" +
      '<div class="listitem__sub"' + (note ? "" : ' style="display:none"') + ">" + escapeHtml(note) + "</div>" + captured +
      '<div class="meta">Expires <span data-testid="authorization-expires-' + escapeHtml(id) + '">' + escapeHtml(item.expires_at) + "</span></div></div>" +
      '<div class="listitem__side"><span class="money money--big" data-testid="authorization-amount-' + escapeHtml(id) + '">' + escapeHtml(money(item.amount)) + "</span>" + actions + "</div></li>";
  }

  function renderAuthorizations(items) {
    var list = q("authorization-list");
    var empty = q("empty-authorizations");
    var sorted = (items || []).slice().sort(function (a, b) {
      return String(b.created_at || "").localeCompare(String(a.created_at || ""));
    });
    if (list) {
      list.innerHTML = sorted.map(authorizationItem).join("");
      list.hidden = sorted.length === 0;
    }
    if (empty) empty.hidden = sorted.length !== 0;
  }

  function refreshAuthorizations() {
    var n = ++seq.authorizations;
    return api("/authorizations").then(function (res) {
      if (n !== seq.authorizations || !res.ok || !res.data) return;
      renderAuthorizations(res.data.authorizations);
    });
  }

  /* ---------- split shares (mirrors stage-1 section 9) ---------- */

  function splitShares(amount, count) {
    if (!count || count < 1) return [];
    var base = Math.floor(amount / count);
    var remainder = amount % count;
    var shares = [];
    for (var i = 0; i < count; i++) shares.push(base + (i < remainder ? 1 : 0));
    return shares;
  }

  /* ---------- form idempotency lifecycle ---------- */

  function formKey(form, getValues) {
    var key = null;
    var signature = null;
    var dirty = false;
    var succeeded = false;
    ["input", "change"].forEach(function (evt) {
      form.addEventListener(evt, function () { dirty = true; });
    });
    return {
      shouldSkip: function () { return succeeded && !dirty; },
      prepare: function () {
        var sig = JSON.stringify(getValues());
        if (key === null || dirty || sig !== signature) key = newKey();
        signature = sig;
        dirty = false;
        return key;
      },
      commit: function () { succeeded = true; dirty = false; },
    };
  }

  /* ---------- authorize form (shared by home and authorizations screens) ---------- */

  function inputValue(id) {
    var el = document.getElementById(id);
    return el ? el.value : "";
  }

  function bindAuthorizeForm(afterSuccess) {
    var form = document.getElementById("authorize-form");
    if (!form) return;
    var host = document.querySelector('[data-errors="authorize"]');
    var guard = formKey(form, function () {
      return {
        to_handle: inputValue("authorize-handle"),
        amount: inputValue("authorize-amount"),
        note: inputValue("authorize-note"),
        visibility: inputValue("authorize-visibility"),
      };
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (guard.shouldSkip()) return;
      var parsed = parseDecimal(inputValue("authorize-amount"));
      if (!parsed.ok) {
        setAlert({ testid: "authorize-error", message: parsed.error, host: host });
        return;
      }
      clearAlert("authorize-error");
      var key = guard.prepare();
      var body = {
        to_handle: inputValue("authorize-handle").trim(),
        amount: parsed.minor,
        note: inputValue("authorize-note"),
        visibility: inputValue("authorize-visibility"),
      };
      api("/authorizations", { method: "POST", key: key, body: body }).then(function (res) {
        if (res.ok) {
          clearAlert("authorize-error");
          guard.commit();
          if (afterSuccess) afterSuccess();
          return;
        }
        setAlert({ testid: "authorize-error", message: errorMessage(res), host: host });
        refreshWallet();
      });
    });
  }

  function formatTimes(root) {
    (root || document).querySelectorAll("[data-ts]").forEach(function (el) {
      el.textContent = timeText(el.getAttribute("data-ts"));
    });
  }

  /* ---------- delegated actions ---------- */

  var handlers = {};
  function on(action, fn) { handlers[action] = fn; }

  document.addEventListener("click", function (event) {
    var el = event.target.closest ? event.target.closest("[data-action]") : null;
    if (!el) return;
    var action = el.getAttribute("data-action");
    var handler = handlers[action];
    if (handler) { event.preventDefault(); handler(el, event); }
  });

  on("logout", function () {
    var done = function () { window.location.href = "/login"; };
    api("/logout", { method: "POST" }).then(function (res) {
      if (res.ok || res.status === 401 || res.status === 405) return done();
      return api("/logout", { method: "GET" }).then(done, done);
    }, done);
  });

  window.PF = {
    config: config,
    money: money,
    decimalString: decimalString,
    parseDecimal: parseDecimal,
    errorMessage: errorMessage,
    newKey: newKey,
    api: api,
    escapeHtml: escapeHtml,
    q: q,
    atHandle: atHandle,
    timeText: timeText,
    setAlert: setAlert,
    clearAlert: clearAlert,
    applyWallet: applyWallet,
    refreshWallet: refreshWallet,
    renderActivity: renderActivity,
    refreshFeed: refreshFeed,
    renderRequests: renderRequests,
    refreshRequests: refreshRequests,
    renderAuthorizations: renderAuthorizations,
    refreshAuthorizations: refreshAuthorizations,
    formKey: formKey,
    splitShares: splitShares,
    bindAuthorizeForm: bindAuthorizeForm,
    formatTimes: formatTimes,
    on: on,
  };
  formatTimes();
})();
