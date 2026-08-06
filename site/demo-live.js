/*
  Turns each demo step into something that actually runs.

  Every panel below calls the same instance the recorded output came from, so a
  reader can check the transcript against the machine rather than taking it on
  trust. Where a step concerns something written into the catalog, it links to
  the entity so the write can be inspected in DataHub itself.

  The reconstruction is the one measurement too slow to sit in a click. It fits
  models over 244k rows and takes about forty seconds, so it is started when the
  page loads and is finished by the time anyone reaches step three.
*/
(function () {
  var API = "https://api.onenept.com";

  var INCIDENTS_URL =
    "https://datahub.onenept.com/dataset/" +
    encodeURIComponent(
      "urn:li:dataset:(urn:li:dataPlatform:postgres,warehouse.analytics_marts.income_features,PROD)"
    ) +
    "/Incidents";

  var LINEAGE_URL =
    "https://datahub.onenept.com/dataset/" +
    encodeURIComponent(
      "urn:li:dataset:(urn:li:dataPlatform:postgres,warehouse.analytics_marts.income_features,PROD)"
    ) +
    "/Lineage";

  var STEPS = {
    estate: {
      cmd: "tools/graph.py find income_features",
      path: "/api/estate",
      blurb: "Asks the catalog what it holds for this table, across all three platforms.",
    },
    history: {
      cmd: "tools/exposure.py history",
      path: "/api/history",
      blurb: "The recording history. Read from the instance, not from a file in the repo.",
    },
    rebuild: {
      cmd: "tools/reconstruct.py --model income-classifier --repeats 1",
      path: "/api/rebuild",
      poll: true,
      blurb:
        "Fits models over 244,000 rows to see what the classifier can still rebuild. " +
        "Started when this page loaded, because it takes about forty seconds.",
    },
    trace: {
      cmd: "tools/trace.py columns income_features",
      path: "/api/trace",
      blurb: "Walks the column graph back through the dbt sibling to where each column enters.",
      link: { href: LINEAGE_URL, text: "see this lineage in DataHub" },
    },
    writeback: {
      cmd: "tools/incident.py --model income-classifier --policy ecoa",
      path: "/api/incident/preview",
      blurb:
        "A dry run, which is the default. Nothing is written without --raise, and this " +
        "page is not given the token that allows it. The incidents it describes are " +
        "already filed on the instance.",
      link: { href: INCIDENTS_URL, text: "open the filed incidents in DataHub" },
    },
    document: {
      cmd: "tools/complydoc.py --model income-classifier --policy eu_ai_act",
      path: "/api/document",
      pdf: true,
      blurb: "Generates the per run record now and hands back the PDF.",
    },
  };

  function h(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text) n.textContent = text;
    return n;
  }

  function render(mount, key) {
    var step = STEPS[key];
    if (!step) return;

    var box = h("div", "liverun-box");

    var head = h("div", "liverun-head");
    head.appendChild(h("span", "lr-cmd", "$ python " + step.cmd));
    var status = h("span", "lr-status", "");
    head.appendChild(status);
    box.appendChild(head);

    box.appendChild(h("p", "lr-blurb", step.blurb));

    var btn = h("button", "lr-btn", step.pdf ? "Generate it now" : "Run it now");
    var out = h("pre", "lr-out");
    out.hidden = true;

    var actions = h("div", "lr-actions");
    actions.appendChild(btn);
    if (step.link) {
      var a = h("a", "lr-link", step.link.text);
      a.href = step.link.href;
      a.target = "_blank";
      a.rel = "noopener";
      actions.appendChild(a);
    }
    box.appendChild(actions);
    box.appendChild(out);
    mount.appendChild(box);

    function show(text, cls) {
      out.hidden = false;
      out.textContent = text;
      out.className = "lr-out" + (cls ? " " + cls : "");
    }

    function setStatus(text, cls) {
      status.textContent = text;
      status.className = "lr-status" + (cls ? " " + cls : "");
    }

    function bodyOf(d) {
      if (d.text) return d.text;
      if (d.data) return JSON.stringify(d.data, null, 2);
      return JSON.stringify(d, null, 2);
    }

    if (step.pdf) {
      btn.addEventListener("click", function () {
        setStatus("generating", "working");
        window.open(API + step.path, "_blank", "noopener");
        setStatus("opened in a new tab", "ok");
      });
      return;
    }

    btn.addEventListener("click", function () {
      btn.disabled = true;
      setStatus("running on the instance", "working");
      show("waiting for the instance ...");

      var url = API + step.path;
      var started = Date.now();

      function handle(d) {
        if (step.poll && d.state === "running") {
          setStatus("measuring, " + Math.round(d.elapsed) + "s elapsed", "working");
          show("Fitting models over 244,000 rows.\nThis is the measurement, not a progress bar.");
          setTimeout(function () {
            fetch(url).then(function (r) { return r.json(); }).then(handle).catch(fail);
          }, 4000);
          return;
        }
        btn.disabled = false;
        var secs = d.seconds != null ? d.seconds : ((Date.now() - started) / 1000).toFixed(1);
        if (d.ok === false) {
          setStatus("the instance returned an error", "bad");
          show(bodyOf(d) + (d.stderr ? "\n\n" + d.stderr : ""), "bad");
          return;
        }
        setStatus("ran in " + secs + "s, just now", "ok");
        var body = bodyOf(d);
        if (d.note) body = d.note + "\n\n" + body;
        show(body);
      }

      function fail() {
        btn.disabled = false;
        setStatus("instance unreachable", "bad");
        show(
          "Could not reach the instance.\n\n" +
            "The recorded output above still stands: it was captured from this same\n" +
            "machine and is committed in examples/ in the repository.",
          "bad"
        );
      }

      fetch(url).then(function (r) { return r.json(); }).then(handle).catch(fail);
    });
  }

  var mounts = document.querySelectorAll("[data-run]");
  for (var i = 0; i < mounts.length; i++) {
    render(mounts[i], mounts[i].getAttribute("data-run"));
  }

  // Start the slow one immediately so step three has an answer waiting.
  if (document.querySelector('[data-run="rebuild"]')) {
    fetch(API + "/api/rebuild?repeats=1", { method: "POST" }).catch(function () {});
  }
})();
