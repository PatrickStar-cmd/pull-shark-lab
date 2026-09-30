"""Sequential gh-backed API adapter; no tokens or external services."""
import json
import re
import subprocess
import time


class GitHubTool:
    def __init__(self, repository, base_branch, max_retries, logger):
        self.repository = repository
        self.base_branch = base_branch
        self.max_retries = max_retries
        self.logger = logger
        self.last_write = 0.0

    def api(self, endpoint, method="GET", payload=None):
        for attempt in range(self.max_retries):
            if method != "GET":
                time.sleep(max(0, 1.1 - (time.monotonic() - self.last_write)))
            command = ["gh", "api", "--include", "--method", method, endpoint]
            if payload is not None:
                command += ["--input", "-"]
            result = subprocess.run(
                command, input=json.dumps(payload) if payload is not None else None,
                capture_output=True, text=True, encoding="utf-8", timeout=120,
            )
            if method != "GET":
                self.last_write = time.monotonic()
            normalized = result.stdout.replace("\r\n", "\n")
            headers, separator, body = normalized.partition("\n\n")
            if result.returncode == 0:
                return json.loads(body if separator else normalized)
            error = result.stderr.strip()
            lower = (body + error).lower()
            if "rate limit" in lower or "http 429" in lower:
                retry = re.search(r"(?im)^retry-after:\s*(\d+)", headers)
                reset = re.search(r"(?im)^x-ratelimit-reset:\s*(\d+)", headers)
                depleted = re.search(r"(?im)^x-ratelimit-remaining:\s*0\s*$", headers)
                wait = int(retry.group(1)) if retry else 60 * (2 ** attempt)
                if depleted and reset:
                    wait = max(wait, int(reset.group(1)) - time.time() + 2)
                self.logger.warning("GitHub rate limit; waiting %.0f seconds", wait)
                time.sleep(max(1, wait))
                continue
            if method == "GET" and ("http 5" in lower or result.returncode != 0) and attempt + 1 < self.max_retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"GitHub {method} {endpoint}: {error or body}")
        raise RuntimeError(f"GitHub rate-limit retries exhausted: {endpoint}")

    def find_pr(self, head):
        owner = self.repository.split("/")[0]
        pulls = self.api(f"repos/{self.repository}/pulls?state=all&head={owner}:{head}&base={self.base_branch}")
        return pulls[0] if pulls else None

    def create_pr(self, title, body, head):
        # Reconcile before creation, including after a response is lost.
        existing = self.find_pr(head)
        if existing:
            return existing
        return self.api(f"repos/{self.repository}/pulls", "POST", {
            "title": title, "body": body, "head": head, "base": self.base_branch,
        })

    def confirm_merge(self, number):
        endpoint = f"repos/{self.repository}/pulls/{number}"
        pull = self.api(endpoint)
        if not pull["merged"]:
            if pull["state"] != "open":
                raise RuntimeError(f"PR #{number} is closed without a merge")
            result = self.api(endpoint + "/merge", "PUT", {
                "merge_method": "merge", "sha": pull["head"]["sha"],
            })
            if not result.get("merged"):
                raise RuntimeError(f"PR #{number} merge failed: {result}")
            pull = self.api(endpoint)
        if not pull.get("merged_at"):
            raise RuntimeError(f"PR #{number} has no confirmed merge time")
        return pull
