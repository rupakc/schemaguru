# Security Policy

## Supported versions

Only the latest released minor version receives security fixes.

## Reporting a vulnerability

Please do not open a public issue. Report privately through GitHub's
"Report a vulnerability" button on the repository's Security tab
(https://github.com/rupakc/schemaguru/security/advisories/new).

Include a description, reproduction steps and the affected version. You can expect
an acknowledgement within a few days and a fix or mitigation plan after triage.

## Scope

SchemaGuru reads warehouse metadata and runs read-only, cost-bounded probes. Anything
that lets input inject SQL, bypass bind parameters, write to a warehouse, leak
credentials, or send unmasked sample values to an LLM provider without opt-in is in scope.
