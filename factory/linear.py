"""`factory linear-sync`: copy Linear issues to the GitHub repo their `area:*` label names.

A central, host-side pass. Each Linear issue in a configured state with a mapped area label
gets one GitHub issue per target repo (`Linear: ENG-123` first body line, `needs-triage`), and
the GitHub URL is attached back to the Linear issue. Factory's own triage takes it from there.
The engine is untouched: GitHub stays the execution record.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

from factory import config

API = "https://api.linear.app/graphql"
MARKER = "Linear: "

ISSUES = """query($filter: IssueFilter, $after: String) {
  issues(filter: $filter, first: 100, after: $after) {
    nodes { id identifier title description url
      project { name url }
      labels { nodes { name } }
      attachments { nodes { url } } }
    pageInfo { hasNextPage endCursor }
  }
}"""
ATTACH = """mutation($input: AttachmentCreateInput!) { attachmentCreate(input: $input) { success } }"""


def log(msg: str) -> None:
    print(f"[linear-sync] {msg}", flush=True)


def config_path() -> Path:
    return config.host_config_path().with_name("linear.toml")


def load(path: Path) -> dict:
    data = tomllib.loads(path.read_text())
    areas = data.get("areas")
    if not isinstance(areas, dict) or not areas or not all(isinstance(v, str) for v in areas.values()):
        raise ValueError(f"{path}: [areas] must map label names to repos")
    owner = data.get("owner", "")
    return {
        "team": data.get("team", "ENG"),
        "states": list(data.get("states", ["Backlog", "Todo"])),
        "ignore": set(data.get("ignore", [])),
        "allow_public": set(data.get("allow_public", [])),
        "areas": {k: v if "/" in v else f"{owner}/{v}" for k, v in areas.items()},
    }


def graphql(key: str, query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(API, data=body, headers={
        "Authorization": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if result.get("errors"):
        raise RuntimeError(f"Linear API: {result['errors'][0].get('message')}")
    return result["data"]


def linear_issues(key: str, cfg: dict) -> list[dict]:
    flt = {"team": {"key": {"eq": cfg["team"]}}, "state": {"name": {"in": cfg["states"]}},
           "labels": {"some": {"name": {"in": sorted(cfg["areas"])}}}}
    issues, after = [], None
    while True:
        page = graphql(key, ISSUES, {"filter": flt, "after": after})["issues"]
        issues += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return issues
        after = page["pageInfo"]["endCursor"]


def targets(issue: dict, cfg: dict) -> list[str]:
    """Repos for an issue's mapped area labels, in label order, ignoring cross-repo markers."""
    repos: list[str] = []
    for label in (n["name"] for n in issue["labels"]["nodes"]):
        if label in cfg["ignore"]:
            continue
        if label.startswith("area:") and label not in cfg["areas"]:
            log(f"{issue['identifier']}: unmapped {label}; skipped for that label")
            continue
        repo = cfg["areas"].get(label)
        if repo and repo not in repos:
            repos.append(repo)
    return repos


def gh(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, check=True).stdout


def mirrors(repo: str) -> dict[str, str]:
    """Existing mirrors by Linear identifier. The list endpoint, not search: search lags writes."""
    rows = json.loads(gh("issue", "list", "--repo", repo, "--state", "all", "--limit", "10000",
                         "--json", "url,body"))
    found: dict[str, str] = {}
    for row in rows:
        first = (row.get("body") or "").split("\n", 1)[0].strip()
        if first.startswith(MARKER):
            found.setdefault(first[len(MARKER):].strip(), row["url"])
    return found


def body(issue: dict) -> str:
    lines = [f"{MARKER}{issue['identifier']}", f"Linear URL: {issue['url']}"]
    if project := issue.get("project"):
        lines.append(f"Project: {project['name']} ({project['url']})")
    return "\n".join(lines) + "\n\n" + (issue.get("description") or "").strip() + "\n"


def run_pass(key: str, cfg: dict, dry_run: bool) -> int:
    visibility: dict[str, str] = {}
    index: dict[str, dict[str, str]] = {}
    labelled: set[str] = set()
    failed = 0
    for issue in linear_issues(key, cfg):
        ident = issue["identifier"]
        linked = [n["url"] for n in issue["attachments"]["nodes"]]
        for repo in targets(issue, cfg):
            prefix = f"https://github.com/{repo}/issues/"
            if any(url.startswith(prefix) for url in linked):
                continue
            try:
                if repo not in visibility:
                    visibility[repo] = gh("repo", "view", repo, "--json", "visibility", "-q", ".visibility").strip()
                if visibility[repo] == "PUBLIC" and repo not in cfg["allow_public"]:
                    log(f"{ident} -> {repo}: refused (public repo not in allow_public)")
                    continue
                if repo not in index:
                    index[repo] = mirrors(repo)
                url = index[repo].get(ident)
                if dry_run:
                    log(f"{ident} -> {repo}: would {'attach ' + url if url else 'create'}")
                    continue
                if url is None:
                    if repo not in labelled:
                        color, desc = config.LABELS[config.LABEL_TRIAGE]
                        subprocess.run(["gh", "label", "create", config.LABEL_TRIAGE, "--repo", repo,
                                        "--color", color, "--description", desc],
                                       capture_output=True, check=False)  # exists: leave it alone
                        labelled.add(repo)
                    url = gh("issue", "create", "--repo", repo, "--title", f"{ident}: {issue['title']}",
                             "--label", config.LABEL_TRIAGE, "--body-file", "-", stdin=body(issue)).strip()
                    index[repo][ident] = url
                    log(f"{ident} -> {repo}: created {url}")
                graphql(key, ATTACH, {"input": {"issueId": issue["id"], "url": url,
                                                "title": f"GitHub {repo}#{url.rsplit('/', 1)[-1]}"}})
                log(f"{ident} -> {repo}: attached {url}")
            except (subprocess.CalledProcessError, OSError, RuntimeError, ValueError, KeyError) as exc:
                failed += 1
                detail = getattr(exc, "stderr", None) or exc
                log(f"{ident} -> {repo}: failed ({str(detail).strip()[:300]})")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="factory linear-sync", description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dry-run", action="store_true", help="print what would happen; write nothing")
    parser.add_argument("--config", type=Path, default=None, help="default: $XDG_CONFIG_HOME/factory/linear.toml")
    args = parser.parse_args(argv)
    path = args.config or config_path()
    key = os.environ.get("LINEAR_API_KEY", "")
    if not key:
        print("factory linear-sync: LINEAR_API_KEY is not set", file=sys.stderr)
        return 2
    try:
        cfg = load(path)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"factory linear-sync: {exc}", file=sys.stderr)
        return 2
    try:
        return run_pass(key, cfg, args.dry_run)
    except (OSError, RuntimeError, KeyError) as exc:
        print(f"factory linear-sync: Linear read failed ({exc})", file=sys.stderr)
        return 1
