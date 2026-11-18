"""`factory linear-sync`: routing, fan-out, idempotence, public refusal, dry-run."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from factory import linear

CFG = {
    "team": "ENG", "states": ["Backlog", "Todo"], "ignore": {"area:shared"}, "allow_public": set(),
    "areas": {"area:backend": "org/Backend-API", "area:webplatform": "org/Webplatform"},
}


def issue(labels: list[str], attached: tuple[str, ...] = ()) -> dict:
    return {"id": "uuid-1", "identifier": "ENG-7", "title": "Fix it", "description": "Body",
            "url": "https://linear.app/x/issue/ENG-7", "project": {"name": "P", "url": "https://linear.app/p"},
            "labels": {"nodes": [{"name": n} for n in labels]},
            "attachments": {"nodes": [{"url": u} for u in attached]}}


class Fake:
    """Linear API and `gh` stand-ins that record every write."""

    def __init__(self, issues: list[dict], existing: dict[str, list[dict]] | None = None,
                 visibility: str = "INTERNAL"):
        self.issues, self.existing, self.visibility = issues, existing or {}, visibility
        self.created: list[tuple[str, str, str | None]] = []
        self.attached: list[str] = []

    def graphql(self, key, query, variables):
        if query is linear.ATTACH:
            self.attached.append(variables["input"]["url"])
            return {"attachmentCreate": {"success": True}}
        return {"issues": {"nodes": self.issues, "pageInfo": {"hasNextPage": False, "endCursor": None}}}

    def gh(self, *args, stdin=None):
        repo = args[args.index("--repo") + 1] if "--repo" in args else args[2]
        if args[:2] == ("repo", "view"):
            return self.visibility + "\n"
        if args[:2] == ("issue", "list"):
            return json.dumps(self.existing.get(repo, []))
        if args[:2] == ("issue", "create"):
            title = args[args.index("--title") + 1]
            self.created.append((repo, title, stdin))
            return f"https://github.com/{repo}/issues/{len(self.created)}\n"
        raise AssertionError(args)

    def run(self, fake_cfg=CFG, dry_run=False) -> int:
        with mock.patch.object(linear, "graphql", self.graphql), mock.patch.object(linear, "gh", self.gh), \
                mock.patch.object(linear.subprocess, "run") as label_create:
            code = linear.run_pass("key", fake_cfg, dry_run)
        self.label_calls = label_create.call_count
        return code


class LinearSyncTest(unittest.TestCase):
    def test_fans_out_per_repo_ignoring_cross_repo_and_already_linked(self) -> None:
        fake = Fake([issue(["area:backend", "area:shared", "area:webplatform"],
                           attached=["https://github.com/org/Webplatform/issues/3"])])
        self.assertEqual(fake.run(), 0)
        self.assertEqual([(r, t) for r, t, _ in fake.created], [("org/Backend-API", "ENG-7: Fix it")])
        body = fake.created[0][2]
        self.assertTrue(body.startswith("Linear: ENG-7\n"))
        self.assertIn("Project: P (https://linear.app/p)", body)
        self.assertEqual(fake.attached, ["https://github.com/org/Backend-API/issues/1"])

    def test_existing_marker_attaches_instead_of_duplicating(self) -> None:
        url = "https://github.com/org/Backend-API/issues/42"
        fake = Fake([issue(["area:backend"])],
                    existing={"org/Backend-API": [{"url": url, "body": "Linear: ENG-7\nLinear URL: x"}]})
        self.assertEqual(fake.run(), 0)
        self.assertEqual(fake.created, [])
        self.assertEqual(fake.attached, [url])

    def test_public_repo_is_refused_unless_allowed(self) -> None:
        fake = Fake([issue(["area:backend"])], visibility="PUBLIC")
        fake.run()
        self.assertEqual((fake.created, fake.attached), ([], []))
        fake = Fake([issue(["area:backend"])], visibility="PUBLIC")
        fake.run({**CFG, "allow_public": {"org/Backend-API"}})
        self.assertEqual(len(fake.created), 1)

    def test_dry_run_writes_nothing(self) -> None:
        fake = Fake([issue(["area:backend", "area:webplatform"])])
        self.assertEqual(fake.run(dry_run=True), 0)
        self.assertEqual((fake.created, fake.attached, fake.label_calls), ([], [], 0))

    def test_create_failure_is_reported_and_other_repos_continue(self) -> None:
        fake = Fake([issue(["area:backend", "area:webplatform"])])
        real_gh = fake.gh

        def flaky(*args, stdin=None):
            if args[:2] == ("issue", "create") and "org/Backend-API" in args:
                raise linear.subprocess.CalledProcessError(1, "gh", stderr="boom")
            return real_gh(*args, stdin=stdin)

        fake.gh = flaky
        self.assertEqual(fake.run(), 1)
        self.assertEqual([r for r, _, _ in fake.created], ["org/Webplatform"])


if __name__ == "__main__":
    unittest.main()
