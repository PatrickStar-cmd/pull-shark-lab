"""Sequential GitHub API adapter using the gh credential store."""
import json
import subprocess
import time
import urllib.error
import urllib.request


class GitHubRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if not newurl.startswith("https://api.github.com/"):
            raise RuntimeError("Refusing a GitHub API redirect to another host")
        return super().redirect_request(request, fp, code, message, headers, newurl)


class GitHubTool:
    def __init__(self, repository, base_branch, max_retries, logger):
        self.repository = repository
        self.base_branch = base_branch
        self.max_retries = max_retries
        self.logger = logger
        self.last_write = 0.0
        credential = subprocess.run(["gh", "auth", "token"], capture_output=True,
                                    text=True, check=True)
        self._credential = credential.stdout.strip()
        if not self._credential:
            raise RuntimeError("No GitHub credential available")
        # The local proxy and gh's TLS client stalled during this run. Direct
        # urllib HTTPS works and retains normal certificate verification.
        self.client = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), GitHubRedirectHandler())

    def api(self, endpoint, method="GET", payload=None):
        for attempt in range(self.max_retries):
            if method != "GET":
                time.sleep(max(0, 1.1 - (time.monotonic() - self.last_write)))
            request = urllib.request.Request(
                f"https://api.github.com/{endpoint}", method=method,
                data=json.dumps(payload).encode("utf-8") if payload is not None else None,
                headers={"Authorization": "Bearer " + self._credential,
                         "Accept": "application/vnd.github+json",
                         "Content-Type": "application/json", "User-Agent": "Pull-Shark-Lab"},
            )
            try:
                with self.client.open(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                body = error.read().decode("utf-8", errors="replace")
                headers = error.headers
                status = error.code
            except (OSError, urllib.error.URLError) as error:
                if method == "GET" and attempt + 1 < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                # A failed write may have reached GitHub. Resume reconciles the
                # branch/PR state before repeating it instead of blindly retrying.
                raise RuntimeError(f"GitHub {method} {endpoint}: {error}") from error
            finally:
                if method != "GET":
                    self.last_write = time.monotonic()
            if "rate limit" in body.lower() or status == 429:
                retry = headers.get("Retry-After")
                reset = headers.get("X-RateLimit-Reset")
                depleted = headers.get("X-RateLimit-Remaining") == "0"
                wait = int(retry) if retry else 60 * (2 ** attempt)
                if depleted and reset:
                    wait = max(wait, int(reset) - time.time() + 2)
                self.logger.warning("GitHub rate limit; waiting %.0f seconds", wait)
                time.sleep(max(1, wait))
                continue
            if method == "GET" and status >= 500 and attempt + 1 < self.max_retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"GitHub {method} {endpoint}: HTTP {status}: {body}")
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
