---
name: tidy-downloads
description: Sort a messy Downloads folder into the user's own folder tree with the tidy-downloads command-line tool. Inventory, a few questions, a plan the user reads, a one-file test, moves in batches, a journal-based check, and undo at any time. Never deletes, never overwrites. Use when the user wants to clean up, sort, file or organise their Downloads (or any dump folder), or to rerun a previous tidy-up on new downloads.
---

# Tidy downloads

You drive the `tidy-downloads` CLI. It decides from rules the user owns, writes a plan, and only
moves files when asked. Your job is to turn the user's habits into rules and to keep every step reversible.

## Safety rules (always)

- Never delete a file, never overwrite one. The tool archives exact duplicates and suffixes name clashes `(dl-2)`; do not work around it.
- Never run `apply` before the user has read the plan summary and said yes.
- Moving across volumes (an external disk, a mounted VM folder) is copy then delete. Check the tool can delete in the source first: run `apply --limit 1` and confirm the file left the source. Without that right you get the same file twice.
- Plans, journals and the text cache hold personal file names and text. Never paste them into a public place, an issue or a post. Never commit them.
- `--llm` sends unidentified file names and 300 characters of their text to Claude. Only with the user's explicit agreement, and say so in those words.

## Steps

1. **Install and inventory.** `pipx install git+https://github.com/korbenmeteo/tidy-downloads` (or `pip install .` from the repository). Count files and size in the source, the oldest and newest dates, the top extensions. Say it in one short paragraph.
2. **Read the target tree.** List the first two levels of the target (default `~/Documents`). Reuse the user's folders; do not invent a parallel tree.
3. **Ask three or four questions at most**, only those the tree does not answer:
   - Is there more than one activity (two companies, company and personal)? From what date does one replace the other? This becomes `[[entities]]` with `start` and `end`.
   - Inside each activity, which split by theme (invoices, bank, HR, contracts, decks, data...)? Propose one from what you saw in step 1.
   - Files with no value (installers, generic images, screenshots): archive them under `{archive}/{year}/`, or leave them?
   - One-off clean-up, or a habit (weekly)? For a habit, keep the rules file tidy and short.
4. **Write the rules** at the path `tidy-downloads --help` shows, starting from the bundled `rules.example.toml`. Then `tidy-downloads scan` and `tidy-downloads plan`.
5. **Show the plan summary**: counts and volume per destination, the unidentified list. Use `tidy-downloads explain <file>` on a few surprising rows to show which rule decided. Fix rules and plan again until the user is happy. Aim for under 5 % unidentified, not zero.
6. **Get a clear yes** to apply.
7. **Test on one file**: `tidy-downloads apply --limit 1`. Check the file is at its destination and gone from the source.
8. **Apply in batches**: `tidy-downloads apply --budget 110`, repeated until nothing is left. Each run resumes from the journal.
9. **Check**: the last line of `apply` says how many items are left; it must be 0, or explained. If a run was interrupted, `tidy-downloads reconcile` first.
10. **Clean up**: remove only folders left empty in the source. Tell the user how to undo (`tidy-downloads undo`) and where the journal is.

## Claude Cowork and other sandboxes

Each shell call may be capped (180 s in Cowork) and background processes may not survive.
Use `scan --budget 110` and `apply --budget 110` and call them again until they report nothing left:
the cache and the journal carry the work from one call to the next.
If the source is a mounted folder, the creation date is not readable; the tool relies on the modification date, which in a downloads folder is the download date.

## Fixing rules

- A file in the wrong place: `explain` it, then change the rule that decided, or add a more specific `[[rules]]` entry above it.
- Keywords of 4 letters or fewer match whole words only. Longer keywords match inside words: prefer specific phrases.
- A company name that is also a common word (Apple, Orange) is removed from the name before themes are matched, as long as it is listed in the entity's `aliases`.
- Spreadsheet text is not used to pick a theme: it is too noisy.
