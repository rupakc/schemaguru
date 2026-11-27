/* SchemaGuru playground: the real library, running in Pyodide.
 *
 * Starts lazily (first tab interaction, or when the playground scrolls into view), then loads
 * Pyodide from the CDN the page names, the two wheels this site was built with
 * (assets/playground/wheels.json), the shop store and its manifest, and glue.py. Every result
 * reaches the page through textContent: nothing the library returns is parsed as HTML. */
(function () {
  "use strict";

  var HOME = "/home/pyodide/";

  function byId(id) { return document.getElementById(id); }

  function baseUrl() {
    var script = document.querySelector('script[src*="assets/playground/playground.js"]');
    return script ? script.src.replace(/playground\.js(\?.*)?$/, "") : "../assets/playground/";
  }

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = src;
      s.onload = resolve;
      s.onerror = function () { reject(new Error("could not load " + src)); };
      document.head.appendChild(s);
    });
  }

  function fetchOk(url, kind) {
    return fetch(url).then(function (r) {
      if (!r.ok) { throw new Error("could not download " + url.split("/").pop() + " (HTTP " + r.status + ")"); }
      return kind === "text" ? r.text() : r.arrayBuffer();
    });
  }

  function clear(el) { while (el.firstChild) { el.removeChild(el.firstChild); } }

  function el(tag, text, cls) {
    var node = document.createElement(tag);
    if (text !== undefined) { node.textContent = text; }
    if (cls) { node.className = cls; }
    return node;
  }

  function checked(name) {
    var input = document.querySelector('input[name="' + name + '"]:checked');
    return input ? input.value : null;
  }

  function setChecked(name, value) {
    document.querySelectorAll('input[name="' + name + '"]').forEach(function (i) { i.checked = i.value === value; });
  }

  function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }

  function shortFingerprint(fp) {
    return fp ? String(fp).replace(/^sha256:/, "").slice(0, 12) : "";
  }

  /* A failure the page reports. The glue marks codes that are not SchemaGuru diagnostics
   * (its own input checks, Python exception names) as generic: those read as a plain
   * failure, never as an SGnnn code. */
  function failureText(data) {
    var err = data.error || {};
    if (err.generic) { return "Could not run this: " + (err.message || "unknown error") + "."; }
    return data.text || String(err.code) + " " + String(err.message);
  }

  function show(pre, data) {
    pre.classList.toggle("sg-pg__out--error", !!data.error);
    pre.textContent = data.error ? failureText(data) : data.text;
    return !data.error;
  }

  function mount(root) {
    if (root.dataset.sgPgMounted) { return; }
    root.dataset.sgPgMounted = "1";

    var statusEl = byId("sg-pg-status");
    var unsupported = byId("sg-pg-unsupported");
    var retry = byId("sg-pg-retry");
    var glue = null;
    var manifest = null;
    var started = null;
    var pyodideReady = null;

    var STAGES = 6;
    var progress = byId("sg-pg-progress");
    // a stage that neither finishes nor fails (a hung CDN or fetch) is failed after this long
    var stageTimeoutMs = window.sgPlaygroundStageTimeoutMs || 120000;

    function status(text) { statusEl.textContent = text; }

    function step(n, text) {
      status("Step " + n + " of " + STAGES + ": " + text);
      progress.hidden = false;
      progress.setAttribute("aria-valuenow", String(n));
      progress.firstElementChild.style.width = Math.round((n - 1) / STAGES * 100) + "%";
    }

    function withTimeout(promise) {
      var timer;
      var limit = new Promise(function (resolve, reject) {
        timer = window.setTimeout(function () {
          reject(new Error("no answer after " + Math.round(stageTimeoutMs / 1000) + " s"));
        }, stageTimeoutMs);
      });
      return Promise.race([promise, limit]).finally(function () { window.clearTimeout(timer); });
    }

    function fail(title, reason, canRetry) {
      byId("sg-pg-unsupported-title").textContent = title;
      byId("sg-pg-unsupported-reason").textContent = reason;
      retry.hidden = !canRetry;
      unsupported.hidden = false;
      if (progress) { progress.hidden = true; }
      status(title);
    }

    if (typeof WebAssembly !== "object") {
      fail(
        "This browser cannot run the playground.",
        "It needs WebAssembly, which is missing or turned off here.",
        false
      );
      return;
    }

    function call(name) {
      var args = Array.prototype.slice.call(arguments, 1);
      try {
        return JSON.parse(glue[name].apply(null, args));
      } catch (e) {
        return { error: { code: "error", message: String((e && e.message) || e).split("\n").pop(), generic: true } };
      }
    }

    function stage(n, name, text, fn) {
      return function (value) {
        step(n, text);
        return withTimeout(Promise.resolve().then(function () { return fn(value); })).catch(function (e) {
          var err = new Error((e && e.message) || String(e));
          err.stage = name;
          throw err;
        });
      };
    }

    function boot() {
      var cdn = root.getAttribute("data-pyodide");
      var base = baseUrl();
      var py = null;
      if (!pyodideReady) {
        pyodideReady = Promise.resolve()
          .then(stage(1, "Pyodide", "Loading Python (Pyodide)…", function () {
            return (window.loadPyodide ? Promise.resolve() : loadScript(cdn + "pyodide.js"))
              .then(function () { return window.loadPyodide({ indexURL: cdn }); });
          }))
          .then(stage(2, "packages", "Loading sqlite3 and PyYAML…", function (pyodide) {
            return pyodide.loadPackage(["sqlite3", "pyyaml"]).then(function () { return pyodide; });
          }));
        pyodideReady.catch(function () { pyodideReady = null; });
      }
      return pyodideReady
        .then(stage(3, "wheels", "Installing the schemaguru and factlang wheels…", function (pyodide) {
          py = pyodide;
          return fetchOk(base + "wheels.json", "text").then(function (text) {
            var wheels = JSON.parse(text);
            var names = [wheels.schemaguru, wheels.factlang];
            if (!names[0] || !names[1]) { throw new Error("this build of the site has no wheels"); }
            return Promise.all(names.map(function (n) { return fetchOk(base + n); }));
          }).then(function (buffers) {
            buffers.forEach(function (b) { py.unpackArchive(b, "wheel"); });
          });
        }))
        .then(stage(4, "store", "Loading the shop store…", function () {
          return fetchOk(base + "playground.json", "text").then(function (text) {
            manifest = JSON.parse(text);
            manifest.raw = text;
            return Promise.all([
              fetchOk(base + manifest.store), fetchOk(base + manifest.overlay), fetchOk(base + "glue.py", "text")
            ]);
          }).then(function (parts) {
            py.FS.writeFile(HOME + manifest.store, new Uint8Array(parts[0]));
            py.FS.writeFile(HOME + manifest.overlay, new Uint8Array(parts[1]));
            py.FS.writeFile(HOME + "glue.py", parts[2]);
          });
        }))
        .then(stage(5, "glue", "Starting the playground glue…", function () {
          py.runPython("import glue");
          glue = py.pyimport("glue");
        }))
        .then(stage(6, "open", "Opening the store…", function () {
          var raw = manifest.raw;
          delete manifest.raw;
          var opened = JSON.parse(glue.sg_open(HOME + manifest.store, raw));
          if (opened.error) { throw new Error(failureText(opened)); }
          return opened;
        }));
    }

    function start() {
      if (started) { return started; }
      unsupported.hidden = true;
      started = boot().then(ready).catch(function (e) {
        started = null;
        fail(
          "The playground could not start.",
          "The " + (e.stage || "start-up") + " stage failed: " + ((e && e.message) || e) + ".",
          true
        );
      });
      return started;
    }

    retry.addEventListener("click", start);

    /* ---- the tabs ------------------------------------------------------------------ */

    var precomputed = {};

    function precomputedNote(tab, out) {
      var item = precomputed[tab];
      if (!item) { return false; }
      var panel = byId("sg-pg-panel-" + tab);
      panel.insertBefore(el("p", "precomputed at build time: " + item.reason, "sg-pg__note"), panel.firstChild);
      out.textContent = item.text;
      return true;
    }

    function ask() {
      var q = byId("sg-pg-question").value;
      var data = call("sg_context", q, checked("sg-pg-profile"));
      var meta = byId("sg-pg-ask-meta");
      if (show(byId("sg-pg-ask-out"), data)) {
        meta.textContent = plural(data.tokens, "token") + " · " + plural(data.tables.length, "table") + " · profile " +
          data.profile + " · fingerprint " + shortFingerprint(data.fingerprint);
      } else {
        meta.textContent = "";
      }
    }

    function path() {
      var data = call("sg_path", byId("sg-pg-from").value, byId("sg-pg-to").value);
      var badge = byId("sg-pg-path-badge");
      var note = byId("sg-pg-path-note");
      show(byId("sg-pg-path-out"), data);
      badge.hidden = !!data.error;
      note.textContent = data.error ? "" : (data.sql_note || "") + (data.sql_note ? "." : "");
      if (data.error) { return; }
      var routes = (data.paths || []).length;
      badge.className = "sg-badge " + (data.refusal ? "sg-badge--rose" : routes ? "sg-badge--teal" : "sg-badge--amber");
      badge.textContent = data.refusal ? "refused: " + data.refusal : routes ? routes + (routes === 1 ? " route" : " routes") : "no route";
    }

    function lineage() {
      var depth = Math.min(5, Math.max(1, parseInt(byId("sg-pg-depth").value, 10) || 1));
      byId("sg-pg-depth").value = String(depth);
      show(byId("sg-pg-lineage-out"), call("sg_lineage", byId("sg-pg-subject").value, checked("sg-pg-direction"), depth));
    }

    function privacy() {
      var mode = checked("sg-pg-mode");
      var data = call("sg_mask", byId("sg-pg-privacy-text").value, mode);
      var meta = byId("sg-pg-privacy-meta");
      var table = byId("sg-pg-privacy-table");
      if (show(byId("sg-pg-privacy-out"), data)) {
        // the library sweep's categories, then what the glue counted beside them, in words
        var parts = [plural(data.hits, "value") + " replaced"];
        if (data.categories.length) { parts.push("detected as: " + data.categories.join(", ")); }
        if (data.names) { parts.push(plural(data.names, "person name") + " replaced"); }
        if (data.literals) { parts.push(plural(data.literals, "quoted literal") + " replaced (none mode)"); }
        meta.textContent = parts.join(" · ");
        table.textContent = data.table_block;
        byId("sg-pg-privacy-table-title").textContent =
          "shop.customers as the agent sees it (sampling.llm_values = " + data.mode + ")";
      } else {
        meta.textContent = "";
      }
    }

    /* Line-level diff (longest common subsequence): which lines of each side are not shared. */
    function changedLines(a, b) {
      var n = a.length, m = b.length, i, j;
      var lcs = [];
      for (i = 0; i <= n; i++) { lcs.push(new Array(m + 1).fill(0)); }
      for (i = n - 1; i >= 0; i--) {
        for (j = m - 1; j >= 0; j--) {
          lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
        }
      }
      var inA = new Array(n).fill(true), inB = new Array(m).fill(true);
      i = 0; j = 0;
      while (i < n && j < m) {
        if (a[i] === b[j]) { inA[i] = false; inB[j] = false; i++; j++; }
        else if (lcs[i + 1][j] >= lcs[i][j + 1]) { i++; }
        else { j++; }
      }
      return [inA, inB];
    }

    function paintLines(pre, lines, changed, cls) {
      clear(pre);
      var first = null;
      lines.forEach(function (line, k) {
        var span = el("span", line, changed[k] ? "sg-pg__line " + cls : "sg-pg__line");
        if (changed[k] && !first) { first = span; }
        pre.appendChild(span);
        if (k < lines.length - 1) { pre.appendChild(document.createTextNode("\n")); }
      });
      pre.scrollTop = first ? Math.max(0, first.offsetTop - 24) : 0;
      return first;
    }

    function views(before, after) {
      var a = String(before.text).split("\n"), b = String(after.text).split("\n");
      var diff = changedLines(a, b);
      paintLines(byId("sg-pg-before"), a, diff[0], "sg-pg__line--removed");
      paintLines(byId("sg-pg-after"), b, diff[1], "sg-pg__line--added");
      byId("sg-pg-before-fp").textContent = shortFingerprint(before.fingerprint);
      byId("sg-pg-after-fp").textContent = shortFingerprint(after.fingerprint);
      var removed = diff[0].filter(Boolean).length, added = diff[1].filter(Boolean).length;
      byId("sg-pg-ov-summary").textContent = removed || added
        ? plural(added, "line") + " added or changed, " + plural(removed, "line") + " removed: highlighted below."
        : "Before and After are the same view.";
    }

    function clearViews() {
      ["sg-pg-before", "sg-pg-after", "sg-pg-before-fp", "sg-pg-after-fp", "sg-pg-ov-summary"].forEach(function (id) {
        clear(byId(id));
      });
    }

    function override() {
      var data = call("sg_override", byId("sg-pg-yaml").value, byId("sg-pg-ov-question").value);
      var list = byId("sg-pg-diags");
      clear(list);
      if (data.error) {
        clearViews();
        list.appendChild(el("li", failureText(data), "sg-pg__diag--error"));
        return;
      }
      if (!data.diagnostics.length) { list.appendChild(el("li", "Your overlay added no diagnostics.")); }
      data.diagnostics.forEach(function (d) {
        var li = el("li", null, "sg-pg__diag--" + String(d.severity).toLowerCase());
        li.appendChild(el("code", d.code));
        li.appendChild(document.createTextNode(" " + String(d.severity).toLowerCase() + ": " + d.message));
        list.appendChild(li);
      });
      views(data.before, data.after);
    }

    function reset() {
      var data = call("sg_reset", byId("sg-pg-ov-question").value);
      var list = byId("sg-pg-diags");
      clear(list);
      byId("sg-pg-yaml").value = manifest.override_example;
      if (data.error) {
        clearViews();
        list.appendChild(el("li", failureText(data), "sg-pg__diag--error"));
        return;
      }
      list.appendChild(el("li", "Overlay removed: the view is the site's own again."));
      views(data, data);
    }

    function baseView() {
      var data = call("sg_reset", byId("sg-pg-ov-question").value);
      if (show(byId("sg-pg-before"), data)) {
        byId("sg-pg-before-fp").textContent = shortFingerprint(data.fingerprint);
      }
    }

    function fill(select, values, chosen) {
      values.forEach(function (v) { select.appendChild(el("option", v)); });
      if (chosen && values.indexOf(chosen) >= 0) { select.value = chosen; }
    }

    function chips(container, items, onPick) {
      items.forEach(function (item) {
        var b = el("button", item.label, "sg-chip sg-pg__chip");
        b.type = "button";
        b.addEventListener("click", function () { onPick(item); });
        container.appendChild(b);
      });
    }

    function on(formId, fn) {
      byId(formId).addEventListener("submit", function (e) { e.preventDefault(); fn(); });
    }

    function ready(opened) {
      precomputed = manifest.precomputed || {};
      byId("sg-pg-fts").hidden = opened.search_backend !== "like";
      var tables = opened.tables || manifest.tables;
      fill(byId("sg-pg-from"), tables, "shop.payments");
      fill(byId("sg-pg-to"), tables, "shop.customers");
      fill(byId("sg-pg-subject"), manifest.lineage_subjects, "analytics.revenue_by_segment");
      var first = manifest.questions[0] || "";
      byId("sg-pg-question").value = first;
      byId("sg-pg-ov-question").value = first;
      byId("sg-pg-yaml").value = manifest.override_example;
      var example = manifest.privacy_examples[0];
      byId("sg-pg-privacy-text").value = example ? example.text : "";

      chips(byId("sg-pg-chips"), manifest.questions.map(function (q) { return { label: q }; }), function (item) {
        byId("sg-pg-question").value = item.label;
        ask();
      });
      chips(byId("sg-pg-examples"), manifest.privacy_examples, function (item) {
        byId("sg-pg-privacy-text").value = item.text;
        setChecked("sg-pg-mode", item.mode || "masked");
        privacy();
      });

      on("sg-pg-ask-form", ask);
      on("sg-pg-path-form", path);
      on("sg-pg-lineage-form", lineage);
      on("sg-pg-privacy-form", privacy);
      on("sg-pg-override-form", override);
      byId("sg-pg-reset").addEventListener("click", reset);
      document.querySelectorAll('input[name="sg-pg-profile"]').forEach(function (i) { i.addEventListener("change", ask); });
      document.querySelectorAll('input[name="sg-pg-direction"]').forEach(function (i) { i.addEventListener("change", lineage); });
      document.querySelectorAll('input[name="sg-pg-mode"]').forEach(function (i) { i.addEventListener("change", privacy); });

      root.querySelectorAll("[role=tabpanel] [disabled]").forEach(function (c) { c.disabled = false; });
      // the override tab opens on the site's own view: applying the example here would change
      // what every other tab shows before anyone asked for it
      var runs = { ask: ask, path: path, lineage: lineage, privacy: privacy, override: baseView };
      Object.keys(runs).forEach(function (tab) {
        var outId = tab === "override" ? "sg-pg-before" : "sg-pg-" + tab + "-out";
        if (!precomputedNote(tab, byId(outId))) { runs[tab](); }
      });
      root.classList.add("sg-pg--ready");
      progress.hidden = true;
      status("Ready: schemaguru " + opened.schemaguru + " is running in your browser.");
    }

    /* ---- lazy start ---------------------------------------------------------------- */

    var tablist = root.querySelector("[role=tablist]");
    ["click", "keydown", "focusin"].forEach(function (type) { tablist.addEventListener(type, start); });
    if ("IntersectionObserver" in window) {
      var io = new IntersectionObserver(function (entries) {
        if (entries.some(function (e) { return e.isIntersecting; })) { io.disconnect(); start(); }
      });
      io.observe(root);
    } else {
      start();
    }
  }

  function init() {
    var root = byId("sg-playground");
    if (root) { mount(root); }
  }

  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", init); }
  else { init(); }
  if (window.document$ && typeof window.document$.subscribe === "function") { window.document$.subscribe(init); }
})();
