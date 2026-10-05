/* Pocketful - requests screen: pay, decline, cancel with in-place refresh. */
(function () {
  "use strict";

  function host() { return document.querySelector('[data-errors="requests"]'); }

  function refresh() {
    PF.refreshRequests();
    PF.refreshWallet();
  }

  PF.on("pay-request", function (btn) {
    var id = btn.getAttribute("data-id");
    btn.disabled = true;
    PF.api("/requests/" + encodeURIComponent(id) + "/pay", { method: "POST", key: PF.newKey(), body: {} })
      .then(function (res) {
        btn.disabled = false;
        if (res.ok) {
          PF.clearAlert("request-error");
        } else {
          PF.setAlert({ testid: "request-error", message: PF.errorMessage(res), host: host() });
        }
        refresh();
      });
  });

  PF.on("decline-request", function (btn) {
    var id = btn.getAttribute("data-id");
    btn.disabled = true;
    PF.api("/requests/" + encodeURIComponent(id) + "/decline", { method: "POST" }).then(function (res) {
      btn.disabled = false;
      if (res.ok) PF.clearAlert("request-error");
      else PF.setAlert({ testid: "request-error", message: PF.errorMessage(res), host: host() });
      refresh();
    });
  });

  PF.on("cancel-request", function (btn) {
    var id = btn.getAttribute("data-id");
    btn.disabled = true;
    PF.api("/requests/" + encodeURIComponent(id) + "/cancel", { method: "POST" }).then(function (res) {
      btn.disabled = false;
      if (res.ok) PF.clearAlert("request-error");
      else PF.setAlert({ testid: "request-error", message: PF.errorMessage(res), host: host() });
      refresh();
    });
  });

  PF.refreshRequests();
  PF.refreshWallet();
})();
