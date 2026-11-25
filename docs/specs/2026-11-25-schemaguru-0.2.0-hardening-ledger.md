# SchemaGuru 0.2.0 — hardening ledger

Every item deferred from the 0.1.0 build (spec `2026-11-25-schemaguru-0.2.0-design.md`,
Appendix A, groups A1–A9) has one row here and ends 0.2.0 in exactly one state (spec H1).
`tests/conformance/test_hardening_ledger.py` checks the table: every ID once, in Appendix A
order, and evidence that matches the state.

| State | Evidence |
|---|---|
| `open` | the task that owns it (`Task 3`) |
| `already fixed` | `commit <sha> · test <path>::<name>`: the commit that made the code right and a test proven to fail without that change (one pair per half when an item has several) |
| `fixed` | `test <path>::<name>`: the red-first test of the fixing task |
| `ruled out` | a one-line reason, plus `· docs: <page>` when the item is user-visible |

Audited against `main` at `710e21f` (Task 1). Tasks 2–8 turn the `open` rows into `fixed` or
`ruled out`; an `already fixed` row is not touched again.

| ID | Item | State | Evidence |
|---|---|---|---|
| A1.1 | `Garcia,Maria` (comma-joined name) not masked | fixed | test tests/unit/pii/test_hardening.py::test_comma_joined_name_masked |
| A1.2 | `M. Garcia` (initial + surname) not masked | fixed | test tests/unit/pii/test_hardening.py::test_initial_surname_masked |
| A1.3 | lowercase names (`maria garcia`) not masked | fixed | test tests/unit/pii/test_hardening.py::test_lowercase_name_masked |
| A1.4 | names not in the name list not masked | fixed | test tests/unit/pii/test_hardening.py::test_cued_unlisted_name_masked |
| A1.5 | lone-surname possessives (`Garcia's order`) not masked | fixed | test tests/unit/pii/test_hardening.py::test_surname_possessive_masked |
| A1.6 | addresses inside sentences not swept | fixed | test tests/unit/pii/test_hardening.py::test_address_in_sentence_masked |
| A1.7 | `password=hunter2` (< 8 chars) not masked | already fixed | commit c918616 · test tests/unit/pii/test_hardening.py::test_short_and_glued_credentials_are_masked |
| A1.8 | credential glued to word chars (`x_<key>`); only keyword-adjacent and `x_api_key=` forms are masked, a bare `x_sk_live_…` or `token_ghp_…` in prose still leaks | fixed | test tests/unit/pii/test_hardening.py::test_glued_credential_masked |
| A1.9 | `github_pat_` / `xapp-` prefixes not covered | already fixed | commit 60b51f9 · test tests/unit/pii/test_hardening.py::test_bare_credentials_masked_in_prose |
| A1.10 | zero-padded ids masked as phone (`0012345678901` → `<phone>`) | ruled out | `0012345678901` is the 00 international dialling form, which no rule can tell from a zero-padded id, so recall keeps it `<phone>`; ids with 3+ leading zeros were already kept (pinned by test_zero_padded_id_not_phone) · docs: docs/guide/pii.md |
| A1.11 | path-like values masked as secret (a hex segment in a path → `<secret>`) | ruled out | fixing it would show path text 0.1.0 masked (recall wins) · docs: docs/guide/pii.md |
| A1.12 | version strings masked as `<ip>` (`release 2.10.3.1`) | ruled out | a four-part version is a valid dotted quad, which no rule can tell from an IPv4 address, so recall keeps it `<ip>`; `1.2.3` and `v10.0.1` were already kept (pinned by test_version_not_ip) · docs: docs/guide/pii.md |
| A1.13 | place/brand names masked as names (`Austin Store`) | ruled out | a name list cannot tell a place or brand from a person (`Austin Store`); unmasking them would leak real names, and recall wins · docs: docs/guide/pii.md |
| A1.14 | `password: varchar(255)` masked as a secret | fixed | test tests/unit/pii/test_hardening.py::test_credential_key_with_sql_type_kept |
| A1.15 | `_contains` misses class-range interior chars (`?` vs `[a-f]`) | fixed | test tests/unit/pii/test_policy.py::test_contains_class_range_interior |
| A1.16 | `detectors.py` detector fn typed `object` + `type: ignore` | fixed | test tests/unit/pii/test_hardening.py::test_detectors_have_no_type_ignore |
| A1.17 | docs: hex needs a letter; the `<secret>` / `<address>` tokens | fixed | test tests/conformance/test_docs_examples.py::test_pii_tokens_documented |
| A2.1 | path check only on a FROM item's first token (`FROM s."x.csv"` passes) | fixed | test tests/unit/test_guard.py::test_file_in_any_from_item_refused |
| A2.2 | `.avro` / `.orc` / `.bz2` / `.gzip` not in the path-suffix list | fixed | test tests/unit/test_guard.py::test_more_file_extensions_refused |
| A2.3 | metadata guard: backtick-quoted identifiers falsely rejected (`` `a;b` ``) | ruled out | accepting `;` inside backticks would loosen the metadata guard beyond FactLang's FL703 guard (`;` refused anywhere) and on non-backtick dialects `;` separates statements; plain backtick and bracket identifiers pass, and backtick, `[ident]` and `q'[...]'` quoting variants now stop a quote inside one hiding a statement (test tests/unit/catalog/test_executor.py::test_backtick_identifiers_accepted_hostile_refused) · docs: docs/guide/security.md |
| A2.4 | metadata guard: `SHOW CREATE` falsely rejected | ruled out | accepting `SHOW CREATE` would loosen the metadata guard beyond FactLang's FL703 guard, and no shipped reader sends it |
| A2.5 | metadata guard's quote scanner ignores backslash escapes | already fixed | commit 7fc2423 · test tests/unit/catalog/test_executor.py::test_metadata_guard_backslash_escape_cannot_hide_a_statement |
| A2.6 | `FactLangExecutor` skips the local guard | already fixed | commit efd00fb · test tests/unit/catalog/test_executor.py::test_factlang_executor_adapts_result |
| A2.7 | fullwidth `；` / `－－` pass `check_predicate` | fixed | test tests/unit/draft/test_verify.py::test_fullwidth_separators_refused |
| A2.8 | look-alike quotes via non-Unicode Windows ODBC (InlineConnector) | fixed | test tests/unit/catalog/test_executor.py::test_inline_refuses_quote_lookalikes |
| A2.9 | a raising `tzinfo` surfaces as `RuntimeError`, not SG109 (fixed: it surfaces as `ExecutionError` `FL700 SG109`, like every other inline refusal, not `CatalogError`) | fixed | test tests/unit/catalog/test_executor.py::test_inline_raising_tzinfo_is_sg109 |
| A3.1 | resume does not re-check the scope hash | open | Task 5 |
| A3.2 | schemas created before the cursor are skipped on resume | open | Task 5 |
| A3.3 | no temp-dir cleanup test | open | Task 5 |
| A3.4 | `list_schemas` ignores `page_token` | open | Task 5 |
| A3.5 | explicit-refs crash pushes non-queued tables | open | Task 5 |
| A3.6 | a `queue_push` failure masks the original exception | open | Task 5 |
| A3.7 | keyhash vs profile DISTINCT precedence decided by batch order | open | Task 5 |
| A3.8 | back-off reset untested | open | Task 5 |
| A3.9 | refill with in-flight probes overshoots (untested) | open | Task 5 |
| A3.10 | fused bytes possibly charged N times | open | Task 5 |
| A3.11 | sampled max-per-key cardinality may say 1:1 | open | Task 5 |
| A3.12 | rule-2 id↔id candidate noise | open | Task 5 |
| A3.13 | `flbridge` imports `factlang.semantics.refs` (not public API) | already fixed | commit 35e0c54 · test tests/architecture/test_layers.py::test_factlang_public_modules_only |
| A4.1 | Postgres `NOT VALID` FKs reported enforced | open | Task 6 |
| A4.2 | Postgres unique indexes not reported | open | Task 6 |
| A4.3 | Postgres integration test lacks partitioned / unanalysed tables | open | Task 6 |
| A4.4 | reader `row_count` estimate unlabelled (must be CATALOG provenance, never MEASURED exact) | already fixed | commit c1642f6 · test tests/unit/measure/test_scheduler.py::test_large_table_uses_system_sample · commit d131b6c · test tests/unit/render/test_table_block.py::test_catalog_row_count_is_labelled_catalog_stats |
| A4.5 | Oracle `ALL_PART_TABLES` joined on nullable `c.owner` | open | Task 6 |
| A4.6 | Oracle materialized-view comments | open | Task 6 |
| A4.7 | Snowflake clustering expressions read as names | open | Task 6 |
| A4.8 | Snowflake raises `ValueError`, not `CatalogError` | open | Task 6 |
| A4.9 | Snowflake `SHOW` caps | open | Task 6 |
| A4.10 | BigQuery `_PARTITIONTIME` pseudo-columns | open | Task 6 |
| A4.11 | BigQuery cross-project `TABLE_STORAGE` | open | Task 6 |
| A4.12 | BigQuery region default docs | open | Task 6 |
| A4.13 | BigQuery FK join by constraint name only | open | Task 6 |
| A4.14 | Redshift re-reads schema-wide info per page | open | Task 6 |
| A4.15 | Redshift ignores `changed_since` (capabilities must then say `incremental=False`) | already fixed | commit 5b6fe87 · test tests/unit/catalog/test_readers_warehouse.py::test_redshift_ignores_changed_since_and_says_so |
| A4.16 | `bytes_billed_is_lower_bound` duck-typed | open | Task 6 |
| A4.17 | fixture handlers match SQL by substring | open | Task 6 |
| A4.18 | keyset paging vs collations | open | Task 6 |
| A5.1 | `from_dict` trusts the stored id | fixed | test tests/unit/model/test_facts.py::test_from_dict_rejects_tampered_id |
| A5.2 | `stable_hash` Decimal / date / mixed-key normalisation | fixed | test tests/property/test_model_props.py::test_stable_hash_normalises |
| A5.3 | py3.10 `'Z'` isoformat (`datetime.fromisoformat` in `Fact.from_dict`) | fixed | test tests/unit/model/test_facts.py::test_from_dict_accepts_z_suffix |
| A5.4 | Config env type coercion for tuple fields | already fixed | commit 0d87ec0 · test tests/unit/test_config.py::test_from_env_coerces_tuple_and_scalar_fields |
| A5.5 | `per_glob` mutable in a frozen dataclass | fixed | test tests/unit/test_config.py::test_per_glob_immutable |
| A5.6 | `_value_identity` must fail loudly on a missing identity field | already fixed | commit c1642f6 · test tests/unit/model/test_facts.py::test_missing_identity_field_raises |
| A5.7 | `facts_for` kind filter in Python (push to SQL, index `(subject_key, kind)`) | already fixed | commit fb65b77 · test tests/unit/store/test_sqlite.py::test_facts_for_filters_kinds_in_sql |
| A5.8 | nested transaction without SAVEPOINT | fixed | test tests/unit/store/contract.py::test_nested_transaction_savepoint |
| A5.9 | stale search doc on subject change | fixed | test tests/unit/store/contract.py::test_reindex_on_subject_change |
| A5.10 | fixed `.tmp` export name | fixed | test tests/unit/store/test_export.py::test_concurrent_exports_do_not_clash |
| A5.11 | `iter_schemas` fetchall | fixed | test tests/unit/store/contract.py::test_iter_schemas_streams |
| A5.12 | a COMMIT failure resets the transaction depth | fixed | test tests/unit/store/test_sqlite.py::test_commit_failure_recovers |
| A5.13 | glossary not in the store's search docs | fixed | test tests/unit/store/contract.py::test_glossary_terms_searchable |
| A5.14 | SG702 code only in the message text | fixed | test tests/unit/store/test_migrations.py::test_newer_store_refused_has_code |
| A5.15 | stale non-empty index after an A→B→A search-backend switch | fixed | test tests/unit/store/test_sqlite.py::test_backend_switch_rebuilds_index |
| A5.16 | read-only store with empty `table_docs` gives no diagnostic | fixed | test tests/unit/store/test_sqlite.py::test_readonly_empty_index_warns |
| A5.17 | no byte-equality test for `snapshot_facts` JSON | fixed | test tests/unit/store/contract.py::test_snapshot_bytes_deterministic |
| A5.18 | two JSON encoders (`ensure_ascii` differs) | fixed | test tests/unit/store/test_export.py::test_store_and_export_share_encoder |
| A5.19 | MEASURED/CATALOG fact with `observed_at=None` treated as fresh | fixed | test tests/unit/render/test_table_block.py::test_unknown_age_marked |
| A5.20 | ESTIMATED never STALE | fixed | test tests/unit/knowledge/test_resolve.py::test_estimated_goes_stale |
| A5.21 | superseded ESTIMATED keeps ESTIMATED | fixed | test tests/unit/knowledge/test_resolve.py::test_superseded_estimate_stale |
| A5.22 | DRAFTED ERROR/UNMEASURABLE without the unverified marker | fixed | test tests/unit/render/test_table_block.py::test_draft_error_marked_draft |
| A5.23 | `diff` vs `resolve` tie-break rules differ | fixed | test tests/property/test_model_props.py::test_diff_resolve_same_tiebreak |
| A5.24 | `diff` has no `checks_run` input | fixed | test tests/unit/knowledge/test_diff.py::test_unrun_checks_not_removed |
| A5.25 | `subject_key` helper belongs in `model/` | fixed | test tests/unit/model/test_facts.py::test_subject_key_in_model |
| A5.26 | `test_all_measured_retained_worst_warns` asserts nothing about warnings | fixed | test tests/unit/knowledge/test_resolve.py::test_all_measured_retained_worst_warns |
| A5.27 | no test for a sampled extra also seen exactly | fixed | test tests/unit/knowledge/test_resolve.py::test_sampled_extra_seen_exactly |
| A6.1 | waiver kind/subject not validated | open | Task 7 |
| A6.2 | duplicate YAML keys not diagnosed (last one wins silently) | open | Task 7 |
| A6.3 | glossary `maps_to` accepts column refs (now dropped as an unresolvable table, SG203 with a misleading message) | open | Task 7 |
| A6.4 | `facts_to_overlay` drops non-overlay kinds silently | open | Task 7 |
| A6.5 | list merge by equality duplicates changed joins | open | Task 7 |
| A6.6 | `mkstemp` 0600 + check/replace window | open | Task 7 |
| A6.7 | overlay JOIN values carry only cardinality | open | Task 7 |
| A6.8 | an overlay entry error retracts its previous good fact (document or fix) | open | Task 7 |
| A6.9 | dbt unique count only sees resolvable columns | open | Task 7 |
| A6.10 | `overlays/dbt.py` module docstring missing | open | Task 7 |
| A6.11 | rule requirements merged across rules uncapped | open | Task 7 |
| A6.12 | bare rule ref with a quoted `(` rejected | open | Task 7 |
| A7.1 | per-graph memo not thread-safe (shared `Graph` under the server) | open | Task 8 |
| A7.2 | hop-limit naming capped at 2×hop_limit | open | Task 8 |
| A7.3 | via-success search near the 1 s benchmark bound | open | Task 8 |
| A7.4 | the denylist scanner misses ALLCAPS+CamelCase (`ZORBClient`) | already fixed | commit 47fccb4 · test tests/architecture/test_denylist.py::test_camel_case_and_digit_edges |
| A7.5 | qualified `t.isDeleted` left bare in a live-rows filter | already fixed | commit 65a7c51 · test tests/unit/render/test_fragments.py::test_quote_catalog_names |
| A7.6 | ambiguous case-insensitive filter column left bare without a diagnostic | already fixed | commit 65a7c51 · test tests/unit/render/test_fragments.py::test_ambiguous_filter_column_is_reported |
| A8.1 | `--format json` errors are plain text on stderr | already fixed | commit a269104 · test tests/unit/cli/test_cli.py::test_json_format_errors_are_a_json_object_on_stderr |
| A8.2 | Sampling page states the rows threshold only when bytes are unknown — clarify | already fixed | commit 28b0a17 · test tests/conformance/test_wiki_facts.py::test_sampling_thresholds_stated |
| A8.3 | owner name in the wiki footer | ruled out | the owner is the named MIT copyright holder; intended (spec [call]) |
| A8.4 | clearer error if the wiki repository is missing | already fixed | commit 28b0a17 · test tests/conformance/test_wiki_facts.py::test_wiki_sync_explains_a_missing_wiki_repository |
| A8.5 | wiki-facts negative controls exercise helpers on synthetic input only | open | Task 8 |
| A8.6 | layer walker wiring not proven by a tmp-tree control | open | Task 8 |
| A8.7 | a facade import is reported as "no layer" | open | Task 8 |
| A8.8 | no-op set comprehension in `test_layers` | open | Task 8 |
| A8.9 | nested `TYPE_CHECKING` handling and the I2 guard lack real-tree negative controls | open | Task 8 |
| A8.10 | `tests/test_version.py` `__all__` assertion (check still valid) | already fixed | commit c603a11 · test tests/test_version.py::test_version |
| A8.11 | the denylist CI job passes silently when the secret is empty | open | Task 8 |
| A9.1 | `InlineConnector` docstring omits the trino `… UTC` form and the databricks zone-less refusal | fixed | test tests/unit/test_connectors.py::test_inline_connector_docstring_names_the_zone_explicit_forms |
| A9.2 | FL507 tests assert only `SG109` (pin `precision 39` / `64-bit`) | fixed | test tests/unit/test_connectors.py::test_fl507_refusals_pin_reason |
| A9.3 | `test_trino_rejects_the_old_timestamp_form` also asserts the zone-less form | fixed | test tests/unit/test_connectors.py::test_trino_rejects_zone_less_timestamp |
| A9.4 | `literal_refusal` became a public name in 0.1.1 | ruled out | renaming would remove a name released in 0.1.1 (public API additive only); kept as is and documented · docs: docs/guide/security.md |
| A9.5 | `check_fact` has no claim cap (SchemaGuru calls `Checker.check` directly, bypassing `max_claims`) | open | Task 8 |
