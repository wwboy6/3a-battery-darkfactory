/* Pocketful - split screen: live preview (mirrors the server share rule) + submit. */
(function () {
  "use strict";

  function value(id) {
    var el = document.getElementById(id);
    return el ? el.value : "";
  }

  function parseHandles(raw) {
    return String(raw || "").split(",").map(function (h) { return h.trim(); }).filter(Boolean);
  }

  function renderPreview() {
    var preview = document.querySelector('[data-testid="split-preview"]');
    var list = document.getElementById("split-preview-list");
    if (!preview || !list) return;
    var handles = parseHandles(value("split-handles"));
    var parsed = PF.parseDecimal(value("split-amount"));
    if (!parsed.ok || handles.length === 0) {
      list.innerHTML = "";
      preview.hidden = true;
      return;
    }
    var shares = PF.splitShares(parsed.minor, handles.length);
    list.innerHTML = handles.map(function (handle, index) {
      return '<div class="preview__row"><span class="preview__handle">' + PF.escapeHtml(handle) + "</span>" +
        '<span class="money" data-testid="split-share-' + PF.escapeHtml(handle) + '">' + PF.escapeHtml(PF.money(shares[index])) + "</span></div>";
    }).join("");
    preview.hidden = false;
  }

  var form = document.getElementById("split-form");
  if (form) {
    var host = document.querySelector('[data-errors="split"]');
    var guard = PF.formKey(form, function () {
      return { amount: value("split-amount"), handles: value("split-handles"), note: value("split-note") };
    });

    ["split-amount", "split-handles"].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) { el.addEventListener("input", renderPreview); el.addEventListener("change", renderPreview); }
    });

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (guard.shouldSkip()) return;
      var parsed = PF.parseDecimal(value("split-amount"));
      if (!parsed.ok) {
        PF.setAlert({ testid: "split-error", message: parsed.error, host: host });
        return;
      }
      var handles = parseHandles(value("split-handles"));
      if (handles.length === 0) {
        PF.setAlert({ testid: "split-error", message: "Add at least one handle to split with.", host: host });
        return;
      }
      PF.clearAlert("split-error");
      var key = guard.prepare();
      var body = { amount: parsed.minor, note: value("split-note"), participant_handles: handles };
      PF.api("/splits", { method: "POST", key: key, body: body }).then(function (res) {
        if (res.ok) {
          PF.clearAlert("split-error");
          guard.commit();
          PF.setAlert({ testid: "split-success", variant: "success", host: host, message: "Split requested." });
          PF.refreshWallet();
          return;
        }
        PF.setAlert({ testid: "split-error", message: PF.errorMessage(res), host: host });
      });
    });

    renderPreview();
  }
})();
