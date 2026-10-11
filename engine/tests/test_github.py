"""Tests for factory_engine.github (task 2.14): pull requests and merges through a fake `gh` on
PATH that records each call."""

import json
import os
import sys
from pathlib import Path

import pytest

from factory_engine.errors import GitHubError
from factory_engine.github import (
    MAX_BODY,
    GitHub,
    MergeApproval,
    PullRequest,
    merge_pr,
    open_pr,
    pr_body,
)

SLUG = "status-json"
SHA = "0123456789abcdef0123456789abcdef01234567"

FAKE_GH = """\
import json, os, sys
args = sys.argv[1:]
entry = {"args": args, "cwd": os.getcwd()}
if "--body-file" in args:
    with open(args[args.index("--body-file") + 1], encoding="utf-8") as handle:
        entry["body"] = handle.read()
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(entry) + "\\n")
if os.environ.get("FAKE_GH_FAIL"):
    sys.stderr.write("GraphQL: Pull request is not mergeable (mergePullRequest)\\n")
    sys.exit(1)
if args[:2] == ["pr", "create"]:
    print(os.environ.get("FAKE_GH_URL", "https://github.com/kunal/pilot/pull/7"))
"""


class FakeGh:
    def __init__(self, tmp_path: Path) -> None:
        self.bin = tmp_path / "fakebin"
        self.bin.mkdir()
        script = self.bin / "fake_gh.py"
        script.write_text(FAKE_GH, encoding="utf-8")
        if sys.platform == "win32":
            launcher = f'@"{sys.executable}" "{script}" %*\r\n'
            (self.bin / "gh.cmd").write_text(launcher, encoding="utf-8")
        else:
            launcher = f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n'
            (self.bin / "gh").write_text(launcher, encoding="utf-8")
            (self.bin / "gh").chmod(0o755)
        self.log = tmp_path / "gh-calls.jsonl"
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_GH_LOG": str(self.log),
        }

    @property
    def gh(self) -> GitHub:
        return GitHub(env=self.env)

    def calls(self) -> list[dict[str, object]]:
        if not self.log.exists():
            return []
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]


@pytest.fixture
def fake(tmp_path: Path) -> FakeGh:
    return FakeGh(tmp_path)


@pytest.fixture
def folders(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_folder = tmp_path / "run"
    run_folder.mkdir()
    return repo, run_folder


def _open(fake: FakeGh, folders: tuple[Path, Path], body: str = "body") -> PullRequest:
    repo, run_folder = folders
    return open_pr(
        fake.gh,
        repo,
        run_folder,
        run_id=3,
        slug=SLUG,
        title="factory(run-3): status json",
        body=body,
    )


# --- pull request ---


def test_open_pr_runs_gh_pr_create_from_the_run_branch_into_main(
    fake: FakeGh, folders: tuple[Path, Path]
) -> None:
    pr = _open(fake, folders)
    assert pr == PullRequest(number=7, url="https://github.com/kunal/pilot/pull/7")
    [call] = fake.calls()
    args = call["args"]
    assert isinstance(args, list)
    assert args[:2] == ["pr", "create"]
    assert args[args.index("--base") + 1] == "main"
    assert args[args.index("--head") + 1] == "factory/run-3-status-json"
    assert args[args.index("--title") + 1] == "factory(run-3): status json"
    assert Path(str(call["cwd"])).resolve() == folders[0].resolve()


def test_the_body_goes_through_a_file_in_the_run_folder(
    fake: FakeGh, folders: tuple[Path, Path]
) -> None:
    body = 'Plan with "quotes" & ampersands | pipes\nand a second line %PATH%\n'
    _open(fake, folders, body=body)
    [call] = fake.calls()
    assert call["body"] == body
    args = call["args"]
    assert isinstance(args, list)
    assert Path(args[args.index("--body-file") + 1]).parent.resolve() == folders[1].resolve()


def test_secrets_are_masked_before_the_body_is_published(
    fake: FakeGh, folders: tuple[Path, Path]
) -> None:
    value = "q8Zt" + "Lm3Rx" + "Vw71Kp"
    _open(fake, folders, body=f"test output:\nDEPLOY_TOKEN={value}\n")
    [call] = fake.calls()
    assert value not in str(call["body"])
    assert "DEPLOY_TOKEN=" in str(call["body"])


def test_an_unexpected_gh_answer_raises(fake: FakeGh, folders: tuple[Path, Path]) -> None:
    fake.env["FAKE_GH_URL"] = "something went sideways"
    with pytest.raises(GitHubError, match="something went sideways"):
        _open(fake, folders)


def test_body_holds_the_plan_test_output_and_findings() -> None:
    body = pr_body(
        run_label="run 3 (status-json)",
        plan="## Steps\n1. Add status.py",
        test_output="5 passed in 0.1s",
        findings="- Minor: rename x",
    )
    for part in ("run 3 (status-json)", "## Steps", "5 passed in 0.1s", "Minor: rename x"):
        assert part in body
    assert body.index("Add status.py") < body.index("5 passed") < body.index("rename x")


def test_long_test_output_keeps_its_end_and_the_body_fits_github() -> None:
    output = "".join(f"line {n}\n" for n in range(200_000)) + "FAILED test_status - boom\n"
    body = pr_body(run_label="run 3", plan="plan", test_output=output, findings="none")
    assert len(body) <= MAX_BODY
    assert "FAILED test_status - boom" in body
    assert "plan" in body
    assert "none" in body


def test_a_huge_plan_still_fits() -> None:
    body = pr_body(run_label="run 3", plan="p" * 200_000, test_output="ok", findings="f" * 200_000)
    assert len(body) <= MAX_BODY


# --- merge ---


def test_merge_runs_gh_pr_merge_pinned_to_the_tested_commit(
    fake: FakeGh, folders: tuple[Path, Path]
) -> None:
    pr = PullRequest(number=7, url="https://github.com/kunal/pilot/pull/7")
    merge_pr(fake.gh, folders[0], pr, head_sha=SHA, approval=MergeApproval(pr_number=7))
    [call] = fake.calls()
    assert call["args"] == ["pr", "merge", "7", "--merge", "--match-head-commit", SHA]


def test_merge_without_an_approval_for_this_pr_raises_and_calls_nothing(
    fake: FakeGh, folders: tuple[Path, Path]
) -> None:
    pr = PullRequest(number=7, url="https://github.com/kunal/pilot/pull/7")
    with pytest.raises(GitHubError, match="approv"):
        merge_pr(fake.gh, folders[0], pr, head_sha=SHA, approval=MergeApproval(pr_number=8))
    assert fake.calls() == []


@pytest.mark.parametrize("sha", ["", "main", "HEAD", "0123", "--admin", SHA.upper() + "x"])
def test_merge_needs_a_full_commit_id(fake: FakeGh, folders: tuple[Path, Path], sha: str) -> None:
    pr = PullRequest(number=7, url="https://github.com/kunal/pilot/pull/7")
    with pytest.raises(GitHubError):
        merge_pr(fake.gh, folders[0], pr, head_sha=sha, approval=MergeApproval(pr_number=7))
    assert fake.calls() == []


def test_a_failing_gh_raises_with_its_error(fake: FakeGh, folders: tuple[Path, Path]) -> None:
    fake.env["FAKE_GH_FAIL"] = "1"
    pr = PullRequest(number=7, url="https://github.com/kunal/pilot/pull/7")
    with pytest.raises(GitHubError, match="not mergeable"):
        merge_pr(fake.gh, folders[0], pr, head_sha=SHA, approval=MergeApproval(pr_number=7))


def test_gh_missing_from_path_raises_with_what_to_do(tmp_path: Path) -> None:
    gh = GitHub(env={"PATH": str(tmp_path)})
    with pytest.raises(GitHubError, match="gh"):
        gh.run(["auth", "status"], tmp_path)


def test_gh_runs_without_prompts(fake: FakeGh, folders: tuple[Path, Path]) -> None:
    script = fake.bin / "fake_gh.py"
    script.write_text(
        FAKE_GH.replace(
            'entry = {"args": args, "cwd": os.getcwd()}',
            'entry = {"args": args, "cwd": os.getcwd(), '
            '"prompt": os.environ.get("GH_PROMPT_DISABLED")}',
        ),
        encoding="utf-8",
    )
    _open(fake, folders)
    [call] = fake.calls()
    assert call["prompt"] == "1"
