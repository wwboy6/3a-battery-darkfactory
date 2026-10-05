/* Pocketful - authorizations screen: create, capture and void holds. */
(function () {
  "use strict";

  function host() { return document.querySelector('[data-errors="authorization"]'); }

  function refresh() {
    PF.refreshAuthorizations();
    PF.refreshWallet();
  }

  PF.bindAuthorizeForm(function () {
    PF.refreshWallet();
    PF.refreshAuthorizations();
    PF.refreshFeed();
  });

  PF.on("capture-authorization", function (btn) {
    var id = btn.getAttribute("data-id");
    var input = document.querySelector('[data-testid="authorization-capture-amount-' + id + '"]');
    var parsed = PF.parseDecimal(input ? input.value : "");
    if (!parsed.ok) {
      PF.setAlert({ testid: "authorization-error", message: parsed.error, host: host() });
      return;
    }
    btn.disabled = true;
    PF.api("/authorizations/" + encodeURIComponent(id) + "/capture", {
      method: "POST", key: PF.newKey(), body: { amount: parsed.minor },
    }).then(function (res) {
      btn.disabled = false;
      if (res.ok) PF.clearAlert("authorization-error");
      else PF.setAlert({ testid: "authorization-error", message: PF.errorMessage(res), host: host() });
      refresh();
    });
  });

  PF.on("void-authorization", function (btn) {
    var id = btn.getAttribute("data-id");
    btn.disabled = true;
    PF.api("/authorizations/" + encodeURIComponent(id) + "/void", { method: "POST" }).then(function (res) {
      btn.disabled = false;
      if (res.ok) PF.clearAlert("authorization-error");
      else PF.setAlert({ testid: "authorization-error", message: PF.errorMessage(res), host: host() });
      refresh();
    });
  });

  refresh();
})();
