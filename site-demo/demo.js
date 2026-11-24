/* SchemaGuru interactive demo: client-side only, no external dependencies.
 *
 * Reads shop_export.json (written by scripts/build_demo_export.py from the real library's
 * output on the shop fixture warehouse) and renders three panels:
 *   1. a question box -> the scoped context view (precomputed by the library for the
 *      suggested questions; anything else falls back to an in-browser keyword match that is
 *      labelled as an approximation),
 *   2. a join-graph explorer (SVG, layout precomputed deterministically) showing path search
 *      results and the hub-guard refusal,
 *   3. a before/after prompt comparison.
 */
(function () {
  "use strict";

  var script = document.currentScript;
  var DATA_URL = new URL("shop_export.json", script ? script.src : window.location.href);

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") node.textContent = attrs[k];
      else if (k === "className") node.className = attrs[k];
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { if (c) node.appendChild(c); });
    return node;
  }

  function svg(tag, attrs) {
    var node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    return node;
  }

  function short(name) { return name.replace(/^shop\./, ""); }

  function tokens(text) {
    return (text.toLowerCase().match(/[a-z0-9]+/g) || []).map(function (w) {
      if (w.length > 3 && w.slice(-3) === "ies") return w.slice(0, -3) + "y";
      if (w.length > 3 && w.slice(-1) === "s" && w.slice(-2) !== "ss") return w.slice(0, -1);
      return w;
    });
  }

  var STOP = {the: 1, a: 1, an: 1, of: 1, by: 1, per: 1, and: 1, or: 1, for: 1, in: 1, to: 1,
    how: 1, many: 1, what: 1, which: 1, is: 1, are: 1, was: 1, were: 1, last: 1, month: 1};

  /* ---------- panel 1: question -> view ---------- */

  function questionPanel(data) {
    var listId = "sg-questions";
    var input = el("input", {id: "sg-q", type: "search", list: listId, autocomplete: "off",
      placeholder: "e.g. " + data.questions[0].question, "aria-describedby": "sg-q-help"});
    var datalist = el("datalist", {id: listId}, data.questions.map(function (q) {
      return el("option", {value: q.question});
    }));
    var button = el("button", {type: "submit", className: "md-button md-button--primary", text: "Show context"});
    var meta = el("p", {className: "sg-meta", "aria-live": "polite"});
    var out = el("pre", {className: "sg-view", tabindex: "0", "aria-label": "context view"});
    var chips = el("div", {className: "sg-chips"}, data.questions.slice(0, 6).map(function (q) {
      var b = el("button", {type: "button", className: "sg-chip", text: q.question});
      b.addEventListener("click", function () { input.value = q.question; show(); });
      return b;
    }));
    var form = el("form", {className: "sg-form", role: "search"}, [
      el("label", {"for": "sg-q", text: "Ask the shop warehouse a question"}), input, datalist, button]);

    function approximate(question) {
      var words = tokens(question).filter(function (w) { return !STOP[w]; });
      var scored = data.tables.map(function (t) {
        var nameToks = tokens(t.name);
        var colToks = [];
        t.columns.forEach(function (c) { colToks = colToks.concat(tokens(c.name)); });
        var bodyToks = tokens(t.block);
        var s = 0;
        words.forEach(function (w) {
          if (nameToks.indexOf(w) >= 0) s += 3;
          if (colToks.indexOf(w) >= 0) s += 1;
          if (bodyToks.indexOf(w) >= 0) s += 0.5;
        });
        return {t: t, s: s};
      }).filter(function (x) { return x.s > 0; });
      scored.sort(function (a, b) { return b.s - a.s || (a.t.name < b.t.name ? -1 : 1); });
      return scored.slice(0, 3).map(function (x) { return x.t; });
    }

    function show() {
      var q = input.value.trim();
      if (!q) return;
      var hit = data.questions.filter(function (x) { return x.question.toLowerCase() === q.toLowerCase(); })[0];
      if (hit) {
        out.textContent = hit.view;
        meta.textContent = "SchemaGuru's own view for this question: " + hit.tables.length +
          " table(s), " + hit.tokens + " tokens (budget 4,000), fingerprint " + hit.fingerprint.slice(0, 19) + "…";
        return;
      }
      var found = approximate(q);
      if (!found.length) {
        out.textContent = "No table matches those words. Try one of the suggested questions.";
        meta.textContent = "";
        return;
      }
      out.textContent = found.map(function (t) { return t.block; }).join("\n\n");
      meta.textContent = "Approximation computed in your browser (keyword match over the exported " +
        "table blocks). The library's own scoping — full-text ranking, glossary hits and join " +
        "expansion — is shown for the suggested questions.";
    }

    form.addEventListener("submit", function (e) { e.preventDefault(); show(); });
    input.value = data.questions[0].question;
    show();
    return el("section", {className: "sg-panel", "aria-labelledby": "sg-h1"}, [
      el("h2", {id: "sg-h1", text: "1. Ask a question"}), form, chips, meta, out]);
  }

  /* ---------- panel 2: join graph ---------- */

  function graphPanel(data) {
    var W = 720, H = 520, PAD = 50;
    var names = data.tables.map(function (t) { return t.name; });
    var pos = {};
    names.forEach(function (n) {
      var p = data.layout[n] || [0.5, 0.5];
      pos[n] = [PAD + p[0] * (W - 2 * PAD), PAD + p[1] * (H - 2 * PAD)];
    });
    var hubs = {};
    data.hubs.forEach(function (h) { hubs[h.table] = h.reason; });

    var root = svg("svg", {viewBox: "0 0 " + W + " " + H, role: "group", "aria-labelledby": "sg-h2",
      "class": "sg-graph"});
    var defs = svg("defs");
    var marker = svg("marker", {id: "sg-arrow", viewBox: "0 0 10 10", refX: "10", refY: "5",
      markerWidth: "9", markerHeight: "9", markerUnits: "userSpaceOnUse", orient: "auto-start-reverse"});
    marker.appendChild(svg("path", {d: "M0,0 L10,5 L0,10 z", "class": "sg-arrowhead"}));
    defs.appendChild(marker);
    root.appendChild(defs);

    var edgeEls = [];
    data.edges.forEach(function (e) {
      var a = pos[e.left_table], b = pos[e.right_table];
      if (!a || !b) return;
      var dx = b[0] - a[0], dy = b[1] - a[1], d = Math.sqrt(dx * dx + dy * dy) || 1;
      var r = 24;
      var line = svg("line", {x1: a[0] + dx / d * r, y1: a[1] + dy / d * r, x2: b[0] - dx / d * r,
        y2: b[1] - dy / d * r, "marker-end": "url(#sg-arrow)",
        "class": "sg-edge" + (e.attribution ? " sg-attr" : "") + (e.verdict === "WEAK" ? " sg-weak" : "")});
      var title = svg("title");
      title.textContent = e.from + " → " + e.to + " · " + (e.cardinality || "?") + " · match " +
        pct(e.match_rate) + ", coverage " + pct(e.coverage) + ", fan-in " + num(e.fan_in) +
        (e.attribution ? " · attribution edge" : "") + " · " + e.verdict;
      line.appendChild(title);
      root.appendChild(line);
      edgeEls.push({e: e, node: line});
    });

    var nodeEls = {};
    names.forEach(function (n) {
      var p = pos[n];
      var g = svg("g", {"class": "sg-node" + (hubs[n] ? " sg-hub" : ""), tabindex: "0", role: "button",
        "aria-label": short(n) + (hubs[n] ? " (hub: " + hubs[n] + ")" : "") + ": select as an endpoint",
        transform: "translate(" + p[0] + "," + p[1] + ")"});
      g.appendChild(svg("circle", {r: "22"}));
      var label = svg("text", {y: "38", "text-anchor": "middle"});
      label.textContent = short(n);
      g.appendChild(label);
      if (hubs[n]) {
        var tag = svg("text", {y: "5", "text-anchor": "middle", "class": "sg-hubtag"});
        tag.textContent = "HUB";
        g.appendChild(tag);
      }
      g.addEventListener("click", function () { pick(n); });
      g.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); pick(n); }
      });
      root.appendChild(g);
      nodeEls[n] = g;
    });

    function select(id, label) {
      var s = el("select", {id: id}, names.map(function (n) { return el("option", {value: n, text: short(n)}); }));
      return [el("label", {"for": id, text: label}), s];
    }
    var fromSel = select("sg-from", "From"), toSel = select("sg-to", "To");
    var result = el("pre", {className: "sg-view sg-path", "aria-live": "polite", tabindex: "0"});
    var status = el("p", {className: "sg-meta"});
    var presets = el("div", {className: "sg-chips"}, [
      ["shop.orders", "shop.events", "The wrong join (hub guard)"],
      ["shop.payments", "shop.customers", "A safe route"],
      ["shop.events", "shop.sessions", "Direct join"],
      ["shop.broken_child", "shop.ref_parent", "A broken join"]
    ].map(function (p) {
      var b = el("button", {type: "button", className: "sg-chip", text: p[2]});
      b.addEventListener("click", function () { fromSel[1].value = p[0]; toSel[1].value = p[1]; run(); });
      return b;
    }));
    var nextEnd = 0;

    function pick(n) {
      (nextEnd === 0 ? fromSel[1] : toSel[1]).value = n;
      nextEnd = 1 - nextEnd;
      run();
    }

    function run() {
      var a = fromSel[1].value, b = toSel[1].value;
      Object.keys(nodeEls).forEach(function (n) {
        nodeEls[n].classList.remove("sg-on", "sg-end", "sg-blocked");
      });
      edgeEls.forEach(function (x) { x.node.classList.remove("sg-on", "sg-blocked"); });
      nodeEls[a].classList.add("sg-end");
      nodeEls[b].classList.add("sg-end");
      if (a === b) {
        result.textContent = "SAME_TABLE: " + a + " is both ends; no join is needed";
        status.textContent = "";
        return;
      }
      var res = data.paths[a + "|" + b];
      if (!res) {
        result.textContent = "This pair is not in the export.";
        status.textContent = "";
        return;
      }
      result.textContent = res.text;
      if (res.refusal) {
        status.textContent = "Refused: " + res.refusal + ".";
        if (res.refusal === "HUB_PIVOT") {
          Object.keys(hubs).forEach(function (h) { nodeEls[h].classList.add("sg-blocked"); });
          edgeEls.forEach(function (x) {
            if (x.e.attribution && hubs[x.e.right_table]) x.node.classList.add("sg-blocked");
          });
        }
        return;
      }
      status.textContent = res.tables.length + " route(s), best first.";
      var best = res.tables[0] || [];
      best.forEach(function (n) { if (nodeEls[n]) nodeEls[n].classList.add("sg-on"); });
      for (var i = 0; i + 1 < best.length; i++) {
        edgeEls.forEach(function (x) {
          var u = x.e.left_table, v = x.e.right_table;
          if ((u === best[i] && v === best[i + 1]) || (v === best[i] && u === best[i + 1])) x.node.classList.add("sg-on");
        });
      }
    }

    fromSel[1].value = "shop.orders";
    toSel[1].value = "shop.events";
    fromSel[1].addEventListener("change", run);
    toSel[1].addEventListener("change", run);
    run();

    var legend = el("p", {className: "sg-meta", text: "Arrows point child → parent. Dashed: an attribution " +
      "edge (fan-in > 10 child rows per parent key). Dotted: a WEAK join (match below 95%). " +
      "Click or press Enter on two tables, or use the selectors. Hover an edge for its numbers."});
    return el("section", {className: "sg-panel", "aria-labelledby": "sg-h2"}, [
      el("h2", {id: "sg-h2", text: "2. Join-graph explorer"}), presets,
      el("div", {className: "sg-form"}, fromSel.concat(toSel)), status, result,
      el("div", {className: "sg-graphwrap"}, [root]), legend]);
  }

  /* ---------- panel 3: before / after ---------- */

  function comparePanel(data) {
    var sel = el("select", {id: "sg-cmp"}, data.questions.map(function (q, i) {
      return el("option", {value: String(i), text: q.question});
    }));
    var before = el("pre", {className: "sg-view", tabindex: "0", "aria-label": "schema dump"});
    var after = el("pre", {className: "sg-view", tabindex: "0", "aria-label": "SchemaGuru view"});
    var hb = el("h3", {}), ha = el("h3", {});
    function show() {
      var q = data.questions[Number(sel.value)];
      before.textContent = data.schema_dump;
      after.textContent = q.view;
      hb.textContent = "Before: the whole schema, pasted (" + data.schema_dump_tokens + " tokens)";
      ha.textContent = "After: guru.context(…) (" + q.tokens + " tokens, " + q.tables.length + " table(s))";
    }
    sel.addEventListener("change", show);
    show();
    var note = el("p", {className: "sg-meta", text: "The schema dump has every table and no idea what " +
      "joins, which values occur, or which rows are live. The view is question-scoped and states " +
      "what was measured, by which method, and what was declared by a person. Token counts use " +
      "the default chars/4 counter."});
    return el("section", {className: "sg-panel", "aria-labelledby": "sg-h3"}, [
      el("h2", {id: "sg-h3", text: "3. Before and after"}),
      el("div", {className: "sg-form"}, [el("label", {"for": "sg-cmp", text: "Question"}), sel]),
      el("div", {className: "sg-cols"}, [el("div", {}, [hb, before]), el("div", {}, [ha, after])]), note]);
  }

  function pct(x) { return x === null || x === undefined ? "unknown" : (Math.round(x * 10000) / 100) + "%"; }
  function num(x) { return x === null || x === undefined ? "unknown" : String(Math.round(x * 10) / 10); }

  function start() {
    var mount = document.getElementById("sg-demo");
    if (!mount) return;
    fetch(DATA_URL.href).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (data) {
      mount.textContent = "";
      mount.appendChild(el("p", {className: "sg-meta", text: "Knowledge export of the shop warehouse, " +
        "SchemaGuru " + data.schemaguru_version + ", as of " + data.as_of + " (value policy: " +
        data.value_policy + ")."}));
      mount.appendChild(questionPanel(data));
      mount.appendChild(graphPanel(data));
      mount.appendChild(comparePanel(data));
    }).catch(function (err) {
      mount.textContent = "The demo data could not be loaded (" + err.message + "). " +
        "Open this page over http(s), e.g. with `mkdocs serve`.";
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
