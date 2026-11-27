---
title: Playground
description: The real SchemaGuru library running in your browser on a small shop warehouse.
hide:
  - navigation
  - toc
---

# Playground

This is the real SchemaGuru library, the wheel built from this site's commit, running in your
browser through [Pyodide](https://pyodide.org). It serves a small knowledge store of the shop
fixture that was crawled, measured and lineage-parsed when the site was built. Nothing you type
leaves the page, and no warehouse is connected: the only downloads are Python itself from the
Pyodide CDN and this site's own files, a few megabytes on the first visit and cached afterwards.

<div id="sg-playground" class="sg-pg sg-tabs" data-pyodide="https://cdn.jsdelivr.net/pyodide/v0.29.0/full/">
<p id="sg-pg-status" class="sg-pg__status" role="status" aria-live="polite">Python starts when you pick a tab or scroll here.</p>
<div id="sg-pg-unsupported" class="sg-callout sg-callout--amber sg-pg__unsupported" hidden>
<p><strong id="sg-pg-unsupported-title">This browser cannot run the playground.</strong> <span id="sg-pg-unsupported-reason">It needs WebAssembly, which is missing or turned off here.</span></p>
<p>Everything on this page also runs locally: start with <a href="../getting-started/">Getting started</a>, or read the guides on <a href="../guide/scoping/">scoping</a>, <a href="../guide/graph/">join paths</a>, <a href="../guide/lineage/">lineage</a>, <a href="../guide/pii/">PII and value masking</a> and <a href="../guide/overlays/">overlays</a>.</p>
<p><button type="button" id="sg-pg-retry" class="sg-button" hidden>Retry</button></p>
</div>
<noscript><p class="sg-callout sg-callout--amber">The playground needs JavaScript. Everything on this page also runs locally: see <a href="../getting-started/">Getting started</a>.</p></noscript>
<div role="tablist" aria-label="Playground tools">
<button type="button" role="tab" id="sg-pg-tab-ask" aria-controls="sg-pg-panel-ask" aria-selected="true">Ask a question</button>
<button type="button" role="tab" id="sg-pg-tab-path" aria-controls="sg-pg-panel-path" aria-selected="false" tabindex="-1">Join path</button>
<button type="button" role="tab" id="sg-pg-tab-lineage" aria-controls="sg-pg-panel-lineage" aria-selected="false" tabindex="-1">Lineage</button>
<button type="button" role="tab" id="sg-pg-tab-privacy" aria-controls="sg-pg-panel-privacy" aria-selected="false" tabindex="-1">Privacy</button>
<button type="button" role="tab" id="sg-pg-tab-override" aria-controls="sg-pg-panel-override" aria-selected="false" tabindex="-1">Override</button>
</div>
<section role="tabpanel" id="sg-pg-panel-ask" aria-labelledby="sg-pg-tab-ask" class="sg-pg__panel" data-tab="ask">
<p class="sg-pg__hint">Ask a question about the shop. SchemaGuru scopes the catalog to it and renders the view an agent would get, within a 4,000-token budget.</p>
<form class="sg-pg__row" id="sg-pg-ask-form">
<label class="sg-pg__grow"><span>Question</span><input id="sg-pg-question" type="text" maxlength="500" autocomplete="off" disabled></label>
<button type="submit" class="sg-button sg-button--solid" disabled>Ask</button>
</form>
<fieldset class="sg-pg__seg" id="sg-pg-profile">
<legend>Profile</legend>
<label><input type="radio" name="sg-pg-profile" value="agent" checked disabled><span>agent</span></label>
<label><input type="radio" name="sg-pg-profile" value="planner" disabled><span>planner</span></label>
<label><input type="radio" name="sg-pg-profile" value="verifier" disabled><span>verifier</span></label>
<label><input type="radio" name="sg-pg-profile" value="router" disabled><span>router</span></label>
</fieldset>
<div class="sg-pg__chips" id="sg-pg-chips" aria-label="Suggested questions"></div>
<p class="sg-pg__note" id="sg-pg-fts" hidden>Search ranking uses the LIKE fallback in this browser (SQLite here has no FTS5).</p>
<p class="sg-pg__meta" id="sg-pg-ask-meta"></p>
<pre class="sg-pg__out" id="sg-pg-ask-out" tabindex="0" aria-label="Context view"></pre>
</section>
<section role="tabpanel" id="sg-pg-panel-path" aria-labelledby="sg-pg-tab-path" class="sg-pg__panel" data-tab="path" hidden>
<p class="sg-pg__hint">Pick two tables. <code>find_join_path</code> returns the measured routes with their support, or a typed refusal when every route would multiply rows.</p>
<form class="sg-pg__row" id="sg-pg-path-form">
<label><span>From</span><select id="sg-pg-from" disabled></select></label>
<label><span>To</span><select id="sg-pg-to" disabled></select></label>
<button type="submit" class="sg-button sg-button--solid" disabled>Find path</button>
</form>
<p class="sg-pg__meta"><span class="sg-badge" id="sg-pg-path-badge" hidden></span> <span id="sg-pg-path-note"></span></p>
<pre class="sg-pg__out" id="sg-pg-path-out" tabindex="0" aria-label="Join path result"></pre>
</section>
<section role="tabpanel" id="sg-pg-panel-lineage" aria-labelledby="sg-pg-tab-lineage" class="sg-pg__panel" data-tab="lineage" hidden>
<p class="sg-pg__hint">Lineage parsed from the shop's analytics views when the site was built, read here by the real <code>lineage</code> tool.</p>
<form class="sg-pg__row" id="sg-pg-lineage-form">
<label class="sg-pg__grow"><span>Table or column</span><select id="sg-pg-subject" disabled></select></label>
<fieldset class="sg-pg__seg">
<legend>Direction</legend>
<label><input type="radio" name="sg-pg-direction" value="upstream" checked disabled><span>upstream</span></label>
<label><input type="radio" name="sg-pg-direction" value="downstream" disabled><span>downstream</span></label>
</fieldset>
<label><span>Depth</span><input id="sg-pg-depth" type="number" min="1" max="5" value="3" disabled></label>
<button type="submit" class="sg-button sg-button--solid" disabled>Trace</button>
</form>
<pre class="sg-pg__out" id="sg-pg-lineage-out" tabindex="0" aria-label="Lineage result"></pre>
</section>
<section role="tabpanel" id="sg-pg-panel-privacy" aria-labelledby="sg-pg-tab-privacy" class="sg-pg__panel" data-tab="privacy" hidden>
<p class="sg-pg__hint">Paste any text and run the masking sweep. In <code>none</code> mode quoted literals are replaced too. The examples use made-up values only.</p>
<div class="sg-pg__chips" id="sg-pg-examples" aria-label="Examples"></div>
<form id="sg-pg-privacy-form">
<label class="sg-pg__block"><span>Text</span><textarea id="sg-pg-privacy-text" rows="3" maxlength="10000" spellcheck="false" disabled></textarea></label>
<div class="sg-pg__row">
<fieldset class="sg-pg__seg">
<legend>Values shown to the model</legend>
<label><input type="radio" name="sg-pg-mode" value="masked" checked disabled><span>masked</span></label>
<label><input type="radio" name="sg-pg-mode" value="none" disabled><span>none</span></label>
</fieldset>
<button type="submit" class="sg-button sg-button--solid" disabled>Mask</button>
</div>
</form>
<p class="sg-pg__meta" id="sg-pg-privacy-meta"></p>
<pre class="sg-pg__out sg-pg__out--short" id="sg-pg-privacy-out" tabindex="0" aria-label="Masked text"></pre>
<h3 class="sg-pg__sub" id="sg-pg-privacy-table-title">shop.customers as the agent sees it</h3>
<pre class="sg-pg__out" id="sg-pg-privacy-table" tabindex="0" aria-labelledby="sg-pg-privacy-table-title"></pre>
</section>
<section role="tabpanel" id="sg-pg-panel-override" aria-labelledby="sg-pg-tab-override" class="sg-pg__panel" data-tab="override" hidden>
<p class="sg-pg__hint">Edit a human overlay and apply it: the person always wins over what was measured or drafted. Your edits live only in this tab and are gone on reload.</p>
<form id="sg-pg-override-form">
<label class="sg-pg__block"><span>Overlay YAML</span><textarea id="sg-pg-yaml" rows="10" maxlength="20000" spellcheck="false" disabled></textarea></label>
<div class="sg-pg__row">
<label class="sg-pg__grow"><span>Question</span><input id="sg-pg-ov-question" type="text" maxlength="500" autocomplete="off" disabled></label>
<button type="submit" class="sg-button sg-button--solid" disabled>Apply</button>
<button type="button" id="sg-pg-reset" class="sg-button" disabled>Reset</button>
</div>
</form>
<ul class="sg-pg__diags" id="sg-pg-diags" aria-label="Diagnostics from your overlay"></ul>
<div class="sg-pg__pair">
<div><h3 class="sg-pg__sub">Before <code class="sg-pg__fp" id="sg-pg-before-fp"></code></h3><pre class="sg-pg__out" id="sg-pg-before" tabindex="0" aria-label="View before the overlay"></pre></div>
<div><h3 class="sg-pg__sub">After <code class="sg-pg__fp" id="sg-pg-after-fp"></code></h3><pre class="sg-pg__out" id="sg-pg-after" tabindex="0" aria-label="View after the overlay"></pre></div>
</div>
</section>
</div>

## What you are looking at

- **Ask a question** calls `guru.context(question, profile=…)`: the same scoped view your agent
  gets. Switch profiles to see how a planner, verifier or router view differs. See
  [Scoping](guide/scoping.md) and [Rendering views](guide/rendering.md).
- **Join path** calls the `find_join_path` tool. SQL snippets for a path appear in the context
  view of a question that needs it. See [Join graph and path search](guide/graph.md).
- **Lineage** calls the `lineage` tool. See [Lineage](guide/lineage.md).
- **Privacy** runs the value-masking sweep. See [PII and value masking](guide/pii.md).
- **Override** loads your YAML as a human overlay on top of the site's own. See
  [Overlays](guide/overlays.md).

To run SchemaGuru on your own warehouse, follow [Getting started](getting-started.md).
