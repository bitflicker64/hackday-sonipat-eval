# Hack Day Sonipat: recording before the 6:30 freeze

This folder records every team's repo each hour from 10 PM on Sat 31 Oct 2026 to 6 AM on Sun 1 Nov,
then freezes one commit per team at 6:30 AM. It uses only git, `gh` and the system Python; it never checks out or runs team code,
and it spends no Claude tokens on reviewing. The review stage after 6:30 is a separate task.

## Setup

1. Put the repo-link form export at `teams.csv` with columns `team_id,problem,repo_url`
   (see `teams.example.csv`). URLs may be `https://github.com/...` or `git@github.com:...`.
2. `gh auth status` must be logged in (5,000 API calls an hour; a round uses about 4 per team).

## Run it on a second Mac (backup)

A second Mac can record the same night independently, so a sleeping laptop or a dropped Wi-Fi
connection on one machine does not cost us the freeze. `AGENTS.md` holds the full setup steps.

1. In the Claude desktop app, open the Code tab, start a session in any folder, and paste:

   ```text
   Clone the GitHub repo bitflicker64/hackday-sonipat-eval into ~/hackday-sonipat-eval using gh,
   move this session into that folder, then read AGENTS.md there and follow it step by step to
   set this Mac up as the backup recorder for Hack Day Sonipat. Ask me for teams.csv when you
   reach that step.
   ```

2. Give it the same `teams.csv` the primary Mac uses when it asks.
3. Keep that Mac awake, plugged in and with the lid open from 9:45 PM to 6:45 AM, with the
   Claude app open.

## Schedule

| Task | When | Runs |
|---|---|---|
| `hackday-record` | Every hour, 10 PM to 6 AM (may start a few minutes late) | `./run.sh` |
| `hackday-freeze` | Sun 1 Nov, 6:30:00, once | `./run.sh --final` |

The window lives in `config.json` (`record_from`, `freeze_at`, local time). `run.sh` refuses a normal
round before 9:45 PM or after the freeze, the final round before 6:29 AM or more than 6 hours after,
and a second freeze once `data/freeze.csv` exists. `--force` skips the time checks, for tests.
The hourly task's cron repeats every day; outside the window its runs only print a refusal.
Disable it after the freeze.

## A round

1. Reads every repo's HEAD with `git ls-remote`, 32 at a time, before anything else.
2. Bare-clones each repo on first sight, fetches after that (`data/mirror/<team>.git`).
3. Records facts about that exact commit, 8 repos at a time.

## What is recorded, per team (`data/rounds/<round>/facts/<team>.json`)

| Group | Fields |
|---|---|
| Identity | team, problem, repo, other teams sharing the repo, branch named in the submitted link, SHA, when it was read, status and why, last SHA seen if the repo has since gone |
| GitHub | public or private, archived, fork, default branch, license (SPDX), created and pushed times |
| PRs | count, merged, open, each PR's number, title, author, merge commit, base, body length, whether the body has a Models section |
| CI on the SHA | GitHub Actions state (pass, fail, pending, none), all checks' state, failing check names, every check (re-runs collapsed) |
| History | commit count, first commit and its files, whether it is HACKATHON.md alone, direct pushes to the default branch after the init commit and their SHAs, commits that came through a PR, author count, last commit time |
| Files | file count, vendored files skipped, HACKATHON.md, README, PR template, license file and its type (MIT, Apache-2.0, other) |
| Examples and tests | `examples/` files, test files, each workflow and whether it runs on push, on PRs, and runs tests |
| Size | non-blank lines of code by extension (lock files and vendored folders left out) |
| Models | open-weight families, closed models, AI SDKs and local endpoints, each with file count, up to 5 `path:line` locations and the names seen |
| Flags | text that tries to instruct a reviewer, with `path:line`; keys in the files at that commit (rule, place, 6-character prefix only; history is not scanned) |
| Change | changed since the last round, new commit count, history rewritten (force push) |

## Statuses

| Status | Meaning |
|---|---|
| `ok` | Recorded in full |
| `init_only` | Only one commit |
| `empty` | Repo exists with no commits |
| `unreachable` | Not found, private (reason says which), or HEAD unreadable after 3 tries |
| `too_large` | Over `max_clone_mb` (1,500 MB) by the API's count; HEAD and API facts recorded, not cloned |
| `fetch_failed` | Clone or fetch failed; HEAD and API facts recorded |
| `record_failed` | The script hit an error on this team; it stays in the round and the freeze with its HEAD |

## Edge cases handled

- `teams.csv` headers like "Team ID", "Problem Statement", "GitHub Repo"; team IDs with spaces or `#`
  (files use a safe name, and two IDs that map to the same name are rejected); blank or duplicate
  IDs and non-GitHub links rejected into `teams_rejected.csv`.
- Links in any letter case, with or without `https://`, with `.git`, `www.`, SSH form, a trailing
  slash, or `/tree/<branch>`. Unlisted headers are matched on a key word ("team", "problem",
  "github" or "repo"). Team IDs over 60 characters are rejected. Rejected rows print a WARNING.
- Two teams on one repo (`shared_repo_with`); a team switching repos mid-night (re-cloned,
  `repo_changed_from`, not reported as a force push).
- This Mac's git logins are ignored, so a private repo is never mistaken for a public one.
- More than 500 PRs: the first 500, newest first, with `truncated_at`.
- CI re-runs collapsed to the newest; Dependabot and other apps kept apart from GitHub Actions.
- Lock files, `node_modules`, `dist` and other vendored folders left out of line counts and model
  search; notebooks count code cells only.
- "You are an AI assistant…" system prompts are not flagged; only text aimed at judges or scores is.
- File names with unicode, quotes or tabs.
- Overlapping runs: a second round is skipped; the freeze reads HEADs at once, then waits for the round.
- The freeze: `freeze.csv` is written straight after the HEAD reads, before any clone, so a crash
  later in the round cannot lose it. HEADs that fail on network errors are retried after 15, 30
  and 60 seconds first. A second freeze can never overwrite the first, even if two run at once.
- A failed GitHub API call records CI as `unknown` (not `none`) and direct pushes as `unknown`.
- A fetch that fails on branch names differing only in case still fetches the frozen commit.
- CSV cells starting with `=`, `+`, `-` or `@` are prefixed with `'` so spreadsheets do not run them.

## Not handled here (for the review stage)

- CI still running at 6:30 shows as `pending`; re-check CI for the frozen SHA later.
- CI that runs only on pull requests shows `none` on the default branch's SHA.
- Rebase-merged PRs: only the last commit links to its PR, so the others count as direct pushes.
- A model name in the code is a mention, not proof it is used.

## Files

| Path | What |
|---|---|
| `data/rounds/<round>/summary.csv` | One row per team, for people |
| `data/rounds/<round>/round.json` | Timing, status counts, teams flagged, tool versions, config hash |
| `data/rounds/<round>/api/<team>/` | Raw GitHub API responses |
| `data/rounds/<round>/teams_rejected.csv` | Rows of `teams.csv` that could not be used, and why |
| `data/history.csv` | One row per team per round: SHA, change, rewrite, commits, PRs, CI |
| `data/latest` | Link to the newest round |
| `data/freeze.csv` | Written once at 6:30, read-only: team, problem, URL, SHA, branch, time read, status |
| `data/freeze.sha256` | Its hash, so later stages can check nobody changed it |
| `data/logs/` | Output of every run |

## Testing

```bash
./run.sh --config tests/config.test.json --force
```

It records three sample repos into `tests/data/`. Delete that folder afterwards.
