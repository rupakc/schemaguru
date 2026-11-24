# Extending SchemaGuru

Every component sits behind a small protocol (a structural type: implement the methods, no
base class needed). Each page here gives the protocol, how to plug an implementation in, a
worked example that runs, and a test template.

| Extension point | Protocol | Plug in with |
|---|---|---|
| [Catalog reader](catalog-reader.md) — a new warehouse | `sg.catalog.CatalogReader` | `sg.catalog.register_reader(dialect, factory)` or `Guru(reader=...)` |
| [Store](store.md) — where knowledge lives | `schemaguru.store.Store` | `Guru(store=...)` |
| [Retriever and embedder](retriever.md) — how tables are found | `sg.scope.Retriever`, `sg.scope.Embedder` | `Guru(retriever=...)` |
| [Token counter](token-counter.md) | `sg.scope.TokenCounter` | `Guru(token_counter=...)` |
| [LLM provider](llm-provider.md) — drafting | `sg.draft.LLMProvider` | `guru.draft(..., provider=...)` |
| [PII detector](pii-detector.md) | `sg.pii.Detector` | `sg.pii.register_detector(d)` |
| [Failure classifier](failure-classifier.md) — runtime feedback | `schemaguru.feedback.FailureClassifier` | `schemaguru.feedback.report_error(..., classifiers=[...])` |
| [Fact kind](fact-kind.md) | `schemaguru.model.KindSpec` | `schemaguru.model.register_fact_kind(spec)` |
| [Render profile and format](profile-format.md) | `sg.render.Profile`, a formatter | `sg.render.register_profile` / `register_format` |
| [Change classifier](change-classifier.md) — refresh | `schemaguru.refresh.ChangeClassifier` | `schemaguru.refresh.register_change_classifier(c)` |
| [Join candidate source](join-candidates.md) | — | overlays and dbt today (see the page) |
| [Server authenticator](authenticator.md) | `schemaguru.server.Authenticator` | `create_app(guru, authenticator=...)` |

!!! note "Discovery"
    The design reserves entry-point groups (`schemaguru.<point>`) for discovering
    extensions from installed packages. 0.1 does not read entry points: register extensions
    in code, as the pages show.

Rules every extension follows, because the rest of SchemaGuru relies on them:

* **Unknown is `None`.** Never return 0, 1.0 or an empty string for something you do not know.
* **One bad item costs one item.** Do not raise on bad data; report it (a `Diagnostic`, or
  leave the item out) and continue.
* **Timestamps are timezone-aware UTC.** Naive datetimes are rejected.
* **Deterministic output.** Equal inputs give equal outputs, in a stable order.
