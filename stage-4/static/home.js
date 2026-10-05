/* Pocketful - home screen: pay, request, authorize, wallet refresh. */
(function () {
  "use strict";

  function value(id) {
    var el = document.getElementById(id);
    return el ? el.value : "";
  }

  function errorsFor(name) {
    return document.querySelector('[data-errors="' + name + '"]');
  }

  function bindPay() {
    var form = document.getElementById("pay-form");
    if (!form) return;
    var host = errorsFor("pay");
    var guard = PF.formKey(form, function () {
      return { to_handle: value("pay-handle"), amount: value("pay-amount"), note: value("pay-note"), visibility: value("pay-visibility") };
    });

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (guard.shouldSkip()) return;
      PF.clearAlert("pay-uncertain");
      var parsed = PF.parseDecimal(value("pay-amount"));
      if (!parsed.ok) {
        PF.setAlert({ testid: "pay-error", message: parsed.error, host: host });
        return;
      }
      PF.clearAlert("pay-error");
      var key = guard.prepare();
      var body = {
        to_handle: value("pay-handle").trim(),
        amount: parsed.minor,
        note: value("pay-note"),
        visibility: value("pay-visibility"),
      };
      PF.api("/payments", { method: "POST", key: key, body: body }).then(function (res) {
        if (res.networkError) {
          PF.setAlert({ testid: "pay-uncertain", variant: "uncertain", host: host,
            message: "We could not confirm this payment. Retry to check - it will only be sent once." });
          return;
        }
        if (res.ok) {
          PF.clearAlert("pay-error");
          PF.clearAlert("pay-uncertain");
          guard.commit();
          PF.refreshWallet();
          PF.refreshFeed();
          return;
        }
        PF.setAlert({ testid: "pay-error", message: PF.errorMessage(res), host: host });
        PF.refreshWallet();
        PF.refreshFeed();
      });
    });
  }

  function bindRequest() {
    var form = document.getElementById("request-form");
    if (!form) return;
    var host = errorsFor("request");
    var guard = PF.formKey(form, function () {
      return { payer_handle: value("request-handle"), amount: value("request-amount"), note: value("request-note") };
    });

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (guard.shouldSkip()) return;
      var parsed = PF.parseDecimal(value("request-amount"));
      if (!parsed.ok) {
        PF.setAlert({ testid: "request-error", message: parsed.error, host: host });
        return;
      }
      PF.clearAlert("request-error");
      var key = guard.prepare();
      var body = { payer_handle: value("request-handle").trim(), amount: parsed.minor, note: value("request-note") };
      PF.api("/requests", { method: "POST", key: key, body: body }).then(function (res) {
        if (res.ok) { PF.clearAlert("request-error"); guard.commit(); return; }
        PF.setAlert({ testid: "request-error", message: PF.errorMessage(res), host: host });
      });
    });
  }

  PF.on("wallet-refresh", function () {
    PF.refreshWallet();
    PF.refreshFeed();
  });

  bindPay();
  bindRequest();
  PF.bindAuthorizeForm(function () { PF.refreshWallet(); });
  PF.refreshWallet();
  PF.refreshFeed();
})();
