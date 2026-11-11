# sqlgami

sqlgami reads a SQL statement that a language model wrote for a plain-English question and
reports how much confidence the statement deserves, and why. It is a Python library that an
application embeds: given the SQL, the schema, the question and a compiled bundle of synthetic
fixtures and reference queries, it returns a deterministic report in about 50 milliseconds —
findings with codes, a verdict, a confidence value — without connecting to the warehouse or
calling a model at scoring time. The report is advisory: an ordering of statements from more to
less trustworthy, not a guarantee. The first target is BigQuery.

- [Overview presentation](pitch/sqlgami-pitch.html)
- [Release notes — v0.1.0-rc1](https://github.com/cyberaka/sqlgami/blob/main/RELEASE.md): what it is, supported constructs, the measured numbers, known limitations
- [Production runbook](PRODUCTION_RUNBOOK.html): running it beside a live warehouse — what scoring never does, and the step-by-step pilot
- [Agent pickup guide](AGENT_PICKUP.html): the prompt to hand a coding agent that pilots it
- [Guarantees](guarantees.html): what a report does and does not promise
- [Pilot guide](pilot.html): schema, term workbook, references, fixture rules, compile, score
- [Handover for the next engineer](https://github.com/cyberaka/sqlgami/blob/main/HANDOVER.md): architecture, bench, open work
- Architecture decisions: [ADR 0001 — the term catalogue](adr/0001-term-catalogue.html) · [ADR 0002 — deterministic live scoring, models offline](adr/0002-deterministic-live-and-offline-compile.html) · [ADR 0003 — BigQuery through a proof-gated translation](adr/0003-bigquery.html)
- [Source on GitHub](https://github.com/cyberaka/sqlgami) (Apache License 2.0)

Author: Abhinav Anand — [abhinav-anand.com](https://abhinav-anand.com/) · blog [cyberaka.com](https://www.cyberaka.com/) · cyberaka@cyberaka.com
