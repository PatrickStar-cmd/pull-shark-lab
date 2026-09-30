# Pull Shark Lab

Personal GitHub pull request automation lab for **PatrickStar-cmd**.

This repository adapts the sequential workflow from
[itxashancode/Pull-Shark-Automation](https://github.com/itxashancode/Pull-Shark-Automation),
upstream commit `4788bc2b24050b3cc39f92aa661897eed240148c` (MIT license).
The original Git manager and logger are retained. The runner and GitHub API
wrapper are adapted for this lab.

## Configuration

- Target: **128 confirmed merged PRs** in this dedicated repository.
- Base branch: `main`; generated records: `CONTRIBUTIONS.md`.
- Sequential execution with a 5-second pause between PRs and a pause between
  API writes. Rate-limit responses trigger backoff, without rotating identities.
- Uses the existing `gh` credential store. No token files or token printing.
- GitHub API requests use Python HTTPS directly with certificate verification;
  no third-party proxy is used for authenticated API traffic.
- No third-party telemetry, dashboard registration, free proxies or webhooks.
- Progress is saved locally in ignored `state.json`; a pending PR is reconciled
  when the same command is restarted.

## Run

Requires Python 3.8+, Git and an authenticated GitHub CLI. No Python packages
need to be installed for this adapted sequential runner.

```powershell
gh auth status
python main.py --dry-run
python main.py --count 128
```

`--count` is the cumulative run target, not the number of additional PRs.
Create a local file named `STOP` to stop cleanly after the current PR, then
remove it before resuming. The runner verifies each PR is merged before saving
it as completed. GitHub determines achievement eligibility and display timing;
the script only verifies the merged PRs.
