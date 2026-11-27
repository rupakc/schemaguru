/* SchemaGuru docs behaviour: scroll reveal, copy chips, accessible tabs. No dependencies. */
(function () {
  "use strict";

  function reveal() {
    var items = document.querySelectorAll(".sg-reveal");
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce || !("IntersectionObserver" in window)) {
      items.forEach(function (el) { el.classList.add("is-visible"); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add("is-visible"); io.unobserve(e.target); }
      });
    }, { threshold: 0.12 });
    items.forEach(function (el) { io.observe(el); });
  }

  function liveRegion() {
    var live = document.querySelector(".sg-live");
    if (!live) {
      live = document.createElement("div");
      live.className = "sg-live";
      live.setAttribute("aria-live", "polite");
      live.setAttribute("role", "status");
      document.body.appendChild(live);
    }
    return live;
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy") ? resolve() : reject(new Error("copy failed")); }
      catch (err) { reject(err); }
      finally { document.body.removeChild(ta); }
    });
  }

  function copyChips() {
    var live = liveRegion();
    document.querySelectorAll("[data-copy]").forEach(function (btn) {
      if (btn.dataset.sgBound) { return; }
      btn.dataset.sgBound = "1";
      btn.addEventListener("click", function () {
        copyText(btn.getAttribute("data-copy") || "").then(function () {
          btn.classList.add("is-copied");
          live.textContent = "";
          window.setTimeout(function () { live.textContent = "Copied"; }, 30);
          window.setTimeout(function () { btn.classList.remove("is-copied"); }, 1800);
        }, function () { live.textContent = "Copy failed"; });
      });
    });
  }

  function tabs() {
    document.querySelectorAll("[role=tablist]").forEach(function (list) {
      if (list.dataset.sgBound) { return; }
      list.dataset.sgBound = "1";
      var tabEls = Array.prototype.slice.call(list.querySelectorAll("[role=tab]"));
      function panelOf(tab) {
        var id = tab.getAttribute("aria-controls");
        return id ? document.getElementById(id) : null;
      }
      function select(tab, focus) {
        tabEls.forEach(function (t) {
          var on = t === tab;
          t.setAttribute("aria-selected", on ? "true" : "false");
          t.tabIndex = on ? 0 : -1;
          var p = panelOf(t);
          if (p) { p.hidden = !on; }
        });
        if (focus) { tab.focus(); }
      }
      var start = tabEls.filter(function (t) { return t.getAttribute("aria-selected") === "true"; })[0] || tabEls[0];
      if (start) { select(start, false); }
      tabEls.forEach(function (tab, i) {
        tab.addEventListener("click", function () { select(tab, false); });
        tab.addEventListener("keydown", function (e) {
          var next = null;
          if (e.key === "ArrowRight") { next = tabEls[(i + 1) % tabEls.length]; }
          else if (e.key === "ArrowLeft") { next = tabEls[(i - 1 + tabEls.length) % tabEls.length]; }
          else if (e.key === "Home") { next = tabEls[0]; }
          else if (e.key === "End") { next = tabEls[tabEls.length - 1]; }
          if (next) { e.preventDefault(); select(next, true); }
        });
      });
    });
  }

  function init() { reveal(); copyChips(); tabs(); }
  function boot() {
    if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", init); }
    else { init(); }
  }
  boot();
  /* mkdocs-material instant navigation swaps pages without a reload */
  if (window.document$ && window.document$.subscribe) { window.document$.subscribe(init); }
})();
