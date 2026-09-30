"""Local adaptation of the Pull-Shark-Automation sequential workflow."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid

from git_manager import GitManager
from github_tool import GitHubTool
from logger import setup_logger


STOP_REQUESTED = False


def request_stop(*_):
    global STOP_REQUESTED
    STOP_REQUESTED = True


def save_state(state):
    temporary = Path("state.json.tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace("state.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int)
    parser.add_argument("--delay", type=float)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    os.chdir((root / config["repo_path"]).resolve())
    target = args.count if args.count is not None else config["pr_count"]
    delay = args.delay if args.delay is not None else config["delay_seconds"]
    if target < 1 or delay < 1:
        raise RuntimeError("Count must be positive and delay must be at least one second")
    if args.dry_run or config.get("dry_run"):
        print(json.dumps({"repository": config["repository"], "target": target,
                          "delay_seconds": delay, "dry_run": True}, indent=2))
        print("Dry run: no Git, GitHub or state mutations performed.")
        return
    if not config.get("auto_merge"):
        raise RuntimeError("This lab counts confirmed merges; auto_merge must be enabled")
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    logger = setup_logger()
    git = GitManager(config["base_branch"], config["max_retries"], logger)
    gh = GitHubTool(config["repository"], config["base_branch"], config["max_retries"], logger)
    expected_remote = f"https://github.com/{config['repository']}.git"
    remote = git.run(["git", "remote", "get-url", "origin"], check_internet=False).stdout.strip()
    if remote.removesuffix(".git") != expected_remote.removesuffix(".git"):
        raise RuntimeError(f"Unexpected origin: {remote}")
    if git.run(["git", "status", "--porcelain"], check_internet=False).stdout.strip():
        raise RuntimeError("Commit or stash tracked changes before running")
    state_path = Path("state.json")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
        "repository": config["repository"], "last_completed_pr": 0, "merged_prs": [],
    }
    if state["repository"] != config["repository"]:
        raise RuntimeError("State belongs to a different repository")
    state["target"] = target
    save_state(state)
    logger.info("Starting %s: %s/%s merged", config["repository"], state["last_completed_pr"], target)
    while state["last_completed_pr"] < target:
        if STOP_REQUESTED or Path("STOP").exists():
            logger.info("Stopped cleanly; restart the same command to resume")
            break
        index = state["last_completed_pr"] + 1
        if "pending" not in state:
            git.run(["git", "checkout", config["base_branch"]], check_internet=False)
            git.run(["git", "pull", "--ff-only", "origin", config["base_branch"]])
            state["pending"] = {"index": index, "branch": f"automation-{index}-{uuid.uuid4().hex[:8]}", "stage": "planned"}
            save_state(state)
        pending = state["pending"]
        branch = pending["branch"]
        if pending["stage"] == "planned":
            exists = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]).returncode == 0
            if exists:
                git.run(["git", "checkout", branch], check_internet=False)
            elif not git.create_branch(branch):
                raise RuntimeError("Branch creation failed")
            marker = f"## Automated Contribution #{index}\n"
            file = Path(config["readme_file"])
            text = file.read_text(encoding="utf-8")
            if marker not in text:
                file.write_text(text + f"\n---\n\n{marker}\nTimestamp (UTC): {datetime.now(timezone.utc).isoformat()}\nBranch: {branch}\n", encoding="utf-8")
                if not git.commit(str(file), f"docs: record automation contribution #{index}"):
                    raise RuntimeError("Commit failed")
            pending["stage"] = "committed"
            save_state(state)
        if pending["stage"] == "committed":
            if not git.push(branch):
                raise RuntimeError("Push failed")
            pending["stage"] = "pushed"
            save_state(state)
        if pending["stage"] == "pushed":
            pull = gh.create_pr(f"docs: automation record #{index}",
                f"Append automation record {index} to CONTRIBUTIONS.md in this personal learning lab.\n\nGenerated by the locally adapted Pull-Shark-Automation sequential workflow.", branch)
            pending.update(stage="created", number=pull["number"], url=pull["html_url"])
            save_state(state)
            logger.info("CREATED %s", pull["html_url"])
        pull = gh.confirm_merge(pending["number"])
        state["merged_prs"].append({"index": index, "number": pull["number"], "url": pull["html_url"], "merged_at": pull["merged_at"]})
        state["last_completed_pr"] = index
        state.pop("pending")
        save_state(state)
        logger.info("MERGED %s/%s %s", index, target, pull["html_url"])
        if index < target:
            for _ in range(int(delay * 10)):
                if STOP_REQUESTED or Path("STOP").exists():
                    break
                time.sleep(0.1)
    git.run(["git", "checkout", config["base_branch"]], check_internet=False)
    git.run(["git", "pull", "--ff-only", "origin", config["base_branch"]])
    logger.info("FINISHED %s/%s confirmed merged PRs", state["last_completed_pr"], target)


if __name__ == "__main__":
    main()
