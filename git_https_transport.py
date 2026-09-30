"""Direct verified Git smart HTTPS for this repository, without a local proxy."""
import base64
import subprocess
import urllib.request


REPOSITORY = "PatrickStar-cmd/pull-shark-lab"
URL = "https://github.com/" + REPOSITORY + ".git/"
ZERO = "0" * 40


def packet(data):
    return f"{len(data) + 4:04x}".encode("ascii") + data


def packets(data):
    offset = 0
    while offset < len(data):
        length = int(data[offset:offset + 4], 16)
        offset += 4
        if length == 0:
            continue
        if length < 4 or offset + length - 4 > len(data):
            raise RuntimeError("Invalid Git packet framing")
        yield data[offset:offset + length - 4]
        offset += length - 4


def local(*args, data=None):
    return subprocess.run(["git", *args], input=data, capture_output=True, check=True).stdout


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError("Unexpected redirect from Git smart HTTPS")


class GitHTTPSTransport:
    def __init__(self):
        remote = local("config", "--get", "remote.origin.url").decode().strip()
        remote = remote.rstrip("/")
        if (remote[:-4] if remote.endswith(".git") else remote) != "https://github.com/" + REPOSITORY:
            raise RuntimeError("Direct Git HTTPS is restricted to the lab repository")
        credential = subprocess.run(["gh", "auth", "token"], capture_output=True,
                                    text=True, check=True).stdout.strip()
        self.authorization = "Basic " + base64.b64encode(
            ("x-access-token:" + credential).encode()).decode()
        self.client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, data=None, service=None):
        headers = {"Authorization": self.authorization, "User-Agent": "git/2.49.0"}
        if service:
            headers["Content-Type"] = f"application/x-{service}-request"
            headers["Accept"] = f"application/x-{service}-result"
        request = urllib.request.Request(URL + path, data=data, headers=headers)
        with self.client.open(request, timeout=60) as response:
            return response.read()

    def refs(self, service):
        advertisement = self.request("info/refs?service=" + service)
        refs = {}
        for line in packets(advertisement):
            if line.startswith(b"# service="):
                continue
            line = line.split(b"\0", 1)[0].strip()
            if b" " in line:
                sha, name = line.split(b" ", 1)
                if len(sha) == 40:
                    refs[name.decode()] = sha.decode()
        return refs

    def fetch(self, branch="main"):
        refs = self.refs("git-upload-pack")
        sha = refs[f"refs/heads/{branch}"]
        present = subprocess.run(["git", "cat-file", "-e", sha], capture_output=True).returncode == 0
        if not present:
            request = packet(f"want {sha} side-band-64k ofs-delta\n".encode()) + b"0000" + packet(b"done\n")
            result = self.request("git-upload-pack", request, "git-upload-pack")
            pack = bytearray()
            for line in packets(result):
                if line[:1] == b"\x01":
                    pack.extend(line[1:])
                elif line[:1] == b"\x03" or line.startswith(b"ERR "):
                    raise RuntimeError(line.decode(errors="replace"))
            if not pack.startswith(b"PACK"):
                raise RuntimeError("No Git pack received")
            # index-pack validates the received pack checksum and all objects.
            local("index-pack", "--stdin", data=bytes(pack))
        local("update-ref", f"refs/remotes/origin/{branch}", sha)
        return sha

    def push(self, branch):
        ref = f"refs/heads/{branch}"
        sha = local("rev-parse", ref).decode().strip()
        old = self.refs("git-receive-pack").get(ref, ZERO)
        if old != sha:
            revisions = sha + "\n"
            if old != ZERO:
                local("merge-base", "--is-ancestor", old, sha)
                revisions += "^" + old + "\n"
            pack = local("pack-objects", "--stdout", "--revs", data=revisions.encode())
            request = packet(f"{old} {sha} {ref}\0report-status side-band-64k ofs-delta\n".encode()) + b"0000" + pack
            response = self.request("git-receive-pack", request, "git-receive-pack")
            report = bytearray()
            for line in packets(response):
                if line[:1] == b"\x01":
                    report.extend(line[1:])
                elif line[:1] == b"\x03":
                    raise RuntimeError(line.decode(errors="replace"))
            status = b"".join(packets(bytes(report)))
            if b"unpack ok\n" not in status or f"ok {ref}\n".encode() not in status:
                raise RuntimeError("Git push rejected: " + status.decode(errors="replace"))
            if self.refs("git-receive-pack").get(ref) != sha:
                raise RuntimeError("Git push could not be independently confirmed")
        local("update-ref", f"refs/remotes/origin/{branch}", sha)
        local("branch", f"--set-upstream-to=origin/{branch}", branch)
        return sha

    def run(self, command):
        operation = command[1]
        if operation == "push":
            sha = self.push(command[-1])
        else:
            branch = command[-1] if operation == "pull" else "main"
            sha = self.fetch(branch)
            if operation == "pull":
                local("merge", "--ff-only", f"refs/remotes/origin/{branch}")
        return subprocess.CompletedProcess(command, 0, f"{operation} confirmed {sha}\n", "")
