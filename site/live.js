/*
  Fills the landing panel from the instance the checks actually run against.

  The figures are in the HTML as well, as the last recorded values. If the
  instance cannot be reached the page keeps those and says so, rather than
  showing blanks or a spinner that never resolves. A number with no provenance
  is worse than a slightly old one, so the panel always states which it is.
*/
(function () {
  var API = "https://api.onenept.com";
  var TIMEOUT = 7000;

  function el(key) {
    return document.querySelector('[data-live="' + key + '"]');
  }

  function put(key, value) {
    var node = el(key);
    if (node && value !== undefined && value !== null) {
      node.textContent = String(value);
      node.classList.add("is-live");
    }
  }

  function ago(seconds) {
    if (seconds < 90) return "moments ago";
    if (seconds < 5400) return Math.round(seconds / 60) + " minutes ago";
    if (seconds < 172800) return Math.round(seconds / 3600) + " hours ago";
    return Math.round(seconds / 86400) + " days ago";
  }

  function state(text, live) {
    var node = el("state");
    if (!node) return;
    node.textContent = text;
    node.classList.toggle("live", !!live);
    node.classList.toggle("stale", !live);
  }

  function fetchJSON(path) {
    var controller = new AbortController();
    var timer = setTimeout(function () {
      controller.abort();
    }, TIMEOUT);
    return fetch(API + path, { signal: controller.signal })
      .then(function (r) {
        clearTimeout(timer);
        if (!r.ok) throw new Error("http " + r.status);
        return r.json();
      });
  }

  state("checking", false);

  fetchJSON("/api/overview")
    .then(function (d) {
      if (!d || !d.as_of) {
        state("last recorded", false);
        return;
      }
      put("findings", d.findings);
      put("models", d.models_watched);
      put("tools", d.mcp_tools);
      if (d.agree_text) {
        var m = d.agree_text.match(/(\d+\s*\/\s*\d+)/);
        put("agree", m ? m[1].replace(/\s+/g, "") : d.agree_text);
      }
      var age = Math.max(0, Math.floor(Date.now() / 1000) - d.as_of);
      state("live, measured " + ago(age), true);
    })
    .catch(function () {
      state("last recorded", false);
    });
})();
