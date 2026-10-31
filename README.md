# AlwaysMyCare drug index

A weekly index of the marketed products in Health Canada's Drug Product Database, built for the search in
AlwaysMyCare, an iPhone app. The app searches the index on the phone; nothing about the person searching is sent
anywhere.

## Source and licence of the data

Contains information licensed under the
[Open Government Licence – Canada](https://open.canada.ca/en/open-government-licence-canada).

Source: Health Canada, [Drug Product Database](https://www.canada.ca/en/health-canada/services/drugs-health-products/drug-products/drug-product-database.html)
(data extract: drug, company, active ingredient and dosage form files for marketed products).

The index is a reorganisation of that data for searching; it is not published, reviewed or endorsed by Health Canada.
It is not medical advice. For a product's authoritative details, see its listing in the Drug Product Database.

## Never rename, transfer or delete this repository or its organisation

Every shipped build of AlwaysMyCare reads
`https://github.com/Got2-Be-Software/alwaysmycare-drug-index/releases/latest/download/manifest.json`
and accepts a file only from `https://github.com/Got2-Be-Software/alwaysmycare-drug-index/releases/download/…`.
GitHub's documentation says of an organisation: "After changing your organization's name, your old organization name
becomes available for someone else to claim" (Renaming an organization), and of a repository that if a new repository
is created with the original name, "redirects to the renamed repository will no longer work" (Renaming a repository).
Whoever took the old name could then publish under that address, and every install would accept what they publish.
So, while any build that reads this address is in use:

- do not rename, transfer or delete this repository;
- do not rename, transfer or delete the `Got2-Be-Software` organisation;
- to move, ship an app update that reads the new address first, wait until no build in use reads the old one, and
  only then retire this repository (archive it; do not delete it, so its name stays taken).

The same rule is written in AlwaysMyCare's product contract.

## What each release holds

Every Sunday (02:00 UTC) the workflow in `.github/workflows/weekly.yml` downloads the current extract, builds the
index with `scripts/build_dpd_index.py`, checks it with `scripts/check_index.py`, and publishes a release tagged
with the build date (`YYYY-MM-DD`, UTC) holding:

| File | What it is |
|---|---|
| `drugindex.sqlite` | The index: one SQLite FTS5 table, `drugs`, schema version 2 (`PRAGMA user_version`). |
| `manifest.json` | `version` (the tag), `sha256` and `size` of `drugindex.sqlite`, and `url`, that release's own address for it. |
| `build-info.json` | The row count and other figures the next week's check compares against. |

The app reads the latest release's `manifest.json`, downloads the file it names, and uses it only if the answer is
200 from GitHub's hosts, its size and SHA-256 match, it opens as a schema-2 index, and its version is newer than the
copy the phone already has.

Nothing is published when the build fails, when the index is below the floors in `scripts/check_index.py`, when it
has more than a tenth fewer rows than the last published build, or when its version is not today's UTC date. A
release is created as a draft, its files are downloaded back and compared with the manifest, and only then is it
published; then it is read back anonymously the way the app reads it. Published releases are not changed.

The repository keeps three records in `latest/`, committed by the workflow:

- `build-info.json`: the last published build's figures, which the next run's check compares against. Before the
  first run it holds the index AlwaysMyCare bundles (built 2026-08-12; 13,538 rows, 9,687,040 bytes).
- `manifest.json`: the last published manifest (a record; the app reads the release's copy, never this one).
- `last-run.json`: the date and outcome of the last run, committed every run, passed or failed.

## If a published index is wrong

A release that passed the checks but is wrong (for example, Health Canada's extract was damaged in a way the floors do
not catch) cannot be withdrawn by deleting it: "latest" would fall back to an older release, and the app ignores any
version older than the one it has. Phones that took the bad copy keep it until a **newer** release appears, and each
phone looks at most once a week. To replace it:

1. Find the last good release's tag (`gh release list -R Got2-Be-Software/alwaysmycare-drug-index`).
2. On the next UTC day after the bad release (one release per UTC date), run the workflow by hand with
   `republish_from` set to that tag: Actions → Weekly index → Run workflow → republish_from = `<good tag>`. It
   publishes the good release's file, checked against its own manifest, under today's date. Its row count is
   compared with that release's own record, not with the bad release's, so the bad one can't refuse it.
   (`gh workflow run weekly.yml -R Got2-Be-Software/alwaysmycare-drug-index -f republish_from=<good tag>`)
3. Leave the bad release in place, and edit its notes to say it was replaced. Do not delete it.

Each phone takes the replacement at its next weekly check, so a phone can keep the bad copy for up to about eight
days after step 2. Never create a release by hand: it becomes "latest". Dated more than a day after a phone's UTC
date, it is refused, so every phone stops updating until the next run publishes over it; dated today or tomorrow, the
phones take it, unchecked by this workflow.

## Who watches this

GitHub: "In a public repository, scheduled workflows are automatically disabled when no repository activity has
occurred in 60 days." It does not say whether a commit pushed by a workflow counts. And: "Notifications for scheduled
workflows are sent to the user who initially created the workflow. If a different user updates the cron syntax in
the workflow file, subsequent notifications will be sent to that user instead." The schedule is
pushed by the Got2 GitHub App, so no person is emailed when a run fails.

So the AlwaysMyCare work thread watches it (the Owner's ruling of 2026-10-31): while it works, it checks the age
of the latest release (`gh release view -R Got2-Be-Software/alwaysmycare-drug-index --json tagName,publishedAt`) and
that `gh api repos/Got2-Be-Software/alwaysmycare-drug-index/actions/workflows/weekly.yml --jq .state` reads
`active`, and tells the Owner when the latest release is more than 8 days old or the schedule is not active.

## The copy in AlwaysMyCare

AlwaysMyCare keeps a copy of this repository's files at `tools/drug-index-repo/`, reviewed there before any change is
pushed here. Change both together. Before each AlwaysMyCare release, `scripts/refresh_bundled_dpd_index.py` in that
repository replaces the app's bundled index with this repository's newest release, checked as the app checks it.

## Building it yourself

```sh
python3 scripts/build_dpd_index.py          # writes dist/drugindex.sqlite
python3 scripts/check_index.py dist/drugindex.sqlite "$(date -u +%Y-%m-%d)" Got2-Be-Software/alwaysmycare-drug-index latest/build-info.json
```

Needs Python 3.12 and `curl`. The scripts are under the MIT licence (`LICENSE`); the data is under the Open
Government Licence – Canada, as above.
