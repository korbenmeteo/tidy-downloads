# Changelog

## 0.1.0

First public version, extracted from a personal script that sorted 8,722 files.

- Commands: `scan`, `plan` (default), `apply`, `undo`, `reconcile`, `explain`.
- Rules in one TOML file: entities with dates, themes, explicit rules, clients and partners, personal rules, whole folders.
- Never deletes or overwrites; exact duplicates archived; a journal line per move; resumable batches with `--budget`.
- Optional `--llm` suggestions for unidentified files, off by default, asks before sending.
- Claude Code skill and plugin.
