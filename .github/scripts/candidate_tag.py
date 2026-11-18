"""Detect and publish a release candidate from the GitHub Actions event."""

import argparse
import ast
import json
import os
import re
import subprocess
from functools import cache
from pathlib import Path

VERSION_PATH = "messaging/__init__.py"
SERIES = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
SOURCE_VERSION = re.compile(rf"({SERIES})rc([1-9][0-9]*)")
CANDIDATE_TAG = re.compile(rf"v({SERIES})-rc\.([1-9][0-9]*)")


def git(*args, input=None):
    return subprocess.run(
        ["git", *args], input=input, stdout=subprocess.PIPE, check=True
    ).stdout


def commit_id(value):
    if not re.fullmatch(r"[0-9a-f]{40}", value) or value == "0" * 40:
        raise ValueError("Expected a nonzero commit SHA from GitHub Actions.")
    return value


@cache
def source_version(commit):
    source = git("show", f"{commit}:{VERSION_PATH}")
    try:
        module = ast.parse(source)
    except SyntaxError:
        raise ValueError(f"Invalid Python in {VERSION_PATH} at {commit}.") from None

    assignments = [
        node for node in module.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "__version__"
    ]
    stores = [
        node for node in ast.walk(module)
        if isinstance(node, ast.Name) and node.id == "__version__"
        and isinstance(node.ctx, (ast.Store, ast.Del))
    ]
    if len(assignments) == len(stores) == 1:
        value = assignments[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    raise ValueError(f"Expected one literal __version__ assignment in {VERSION_PATH} at {commit}.")


def version_parts(version):
    match = SOURCE_VERSION.fullmatch(version)
    if not match:
        raise ValueError(f"Expected an X.Y.ZrcN candidate in {VERSION_PATH}.")
    series, number = match.groups()
    return series, int(number)


def is_ancestor(ancestor, commit):
    result = subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, commit])
    if result.returncode not in (0, 1):
        result.check_returncode()
    return result.returncode == 0


def version_origin(commit, version):
    """Follow parents with the same version to find where the value was introduced."""
    pending = [commit]
    visited = set()
    origins = set()
    while pending:
        commit = pending.pop()
        if commit in visited:
            continue
        visited.add(commit)
        parents = git("show", "-s", "--format=%P", commit).decode().split()
        matching = [parent for parent in parents if source_version(parent) == version]
        if matching:
            pending.extend(matching)
        else:
            origins.add(commit)
    if len(origins) != 1:
        raise ValueError("The new version has multiple introduction commits in merged history.")
    return origins.pop()


def detect(event, head):
    history = git("rev-list", "--first-parent", head).decode().splitlines()
    if event == "push":
        payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
        before = commit_id(payload["before"])
        if before not in history:
            raise ValueError("The push base is outside the first-parent history of the event commit.")
        pushed = history[:history.index(before)]
        previous = before
        changes = []
        for commit in reversed(pushed):
            if source_version(commit) != source_version(previous):
                changes.append((commit, previous))
            previous = commit
        if not changes:
            print("No __version__ value changed in this push.")
            return
        if len(changes) != 1:
            raise ValueError("Multiple __version__ changes occurred in one push.")
        transition, previous = changes[0]
    elif event == "workflow_dispatch":
        for transition, previous in zip(history, history[1:]):
            if source_version(transition) != source_version(previous):
                break
        else:
            raise ValueError("No version update was found in the first-parent history.")
        before = previous
    else:
        raise ValueError("Expected a push or workflow_dispatch event.")

    version = source_version(transition)
    series, number = version_parts(version)
    commit = version_origin(transition, version)
    if is_ancestor(commit, before):
        raise ValueError("The version introduction already belongs to the history before the update.")
    print(f"Found v{series}-rc.{number} introduced at {commit}.")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        print(f"commit={commit}", f"previous={previous}", sep="\n", file=output)


def remote_candidates(series):
    candidates = {}
    for line in git("ls-remote", "--tags", "origin").splitlines():
        oid, ref = line.decode(errors="surrogateescape").split("\t")
        tag = ref.removeprefix("refs/tags/")
        match = CANDIDATE_TAG.fullmatch(tag.removesuffix("^{}"))
        if match and match[1] == series:
            candidates[tag] = oid
    return candidates


def remote_target(candidates, tag):
    return candidates.get(tag + "^{}", candidates.get(tag))


def changelog(tag, previous_tag, previous, commit):
    fields = git(
        "-c", "core.abbrev=12", "log", "--reverse", "--date-order", "-z",
        "--format=%h%x00%s%x00%b", f"{previous}..{commit}",
    ).split(b"\0")
    if fields.pop():
        raise ValueError("Git returned an incomplete changelog.")
    entries = [f"{tag}\n\nChanges since {previous_tag}\n\n".encode()]
    for sha, subject, body in zip(fields[::3], fields[1::3], fields[2::3], strict=True):
        entry = b"- " + sha
        if subject:
            entry += b" " + subject
        entries.append(entry + b"\n")
        if body:
            entries.extend(b"  " + line + b"\n" if line else b"\n" for line in body.splitlines())
    return b"".join(entries)


def publish(commit, previous_source):
    series, number = version_parts(source_version(commit))
    tag = f"v{series}-rc.{number}"
    ref = f"refs/tags/{tag}"
    candidates = remote_candidates(series)

    def verify_target(target):
        if target != commit:
            raise ValueError(f"Expected {tag} at {commit}, found {target or 'no remote tag'}.")

    def summarize(outcome):
        print(f"{outcome} {tag} at {commit}.")
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
            print(f"{outcome} `{tag}` at `{commit}`.", file=summary)

    target = remote_target(candidates, tag)
    if target:
        verify_target(target)
        summarize("Reused existing candidate")
        return

    previous_version = source_version(previous_source)
    previous_series, previous_number = version_parts(previous_version)
    if previous_series != series or previous_number >= number:
        raise ValueError("A new candidate must advance within the previous source version's series.")
    previous_tag = f"v{series}-rc.{previous_number}"
    prior_numbers = [
        int(match[2]) for name in candidates if (match := CANDIDATE_TAG.fullmatch(name))
    ]
    if not prior_numbers or max(prior_numbers) != previous_number:
        raise ValueError(f"Expected {previous_tag} to be the latest published candidate in this series.")

    git("fetch", "--no-tags", "origin", f"refs/tags/{previous_tag}")
    previous_object = git("rev-parse", "FETCH_HEAD").decode().strip()
    previous = git("rev-parse", "FETCH_HEAD^{commit}").decode().strip()
    if previous_object != candidates[previous_tag] or previous != remote_target(candidates, previous_tag):
        raise ValueError(f"The remote predecessor {previous_tag} changed during verification.")
    if not is_ancestor(previous, commit) or version_origin(previous_source, previous_version) != previous:
        raise ValueError(f"{previous_tag} does not mark the previous version's introduction in the candidate's history.")

    message = changelog(tag, previous_tag, previous, commit)
    git(
        "-c", "user.name=github-actions[bot]",
        "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "tag", "--annotate", "--no-sign", "--cleanup=verbatim", "--file=-", tag, commit,
        input=message,
    )
    annotation = git("cat-file", "tag", ref).split(b"\n\n", 1)[1]
    if annotation != message or git("rev-parse", f"{ref}^{{commit}}").decode().strip() != commit:
        raise ValueError(f"The local annotation or commit for {tag} differs from the expected value.")
    tag_object = git("rev-parse", ref).decode().strip()

    latest = remote_candidates(series)
    target = remote_target(latest, tag)
    if target:
        verify_target(target)
        summarize("Reused existing candidate")
        return
    if latest != candidates:
        raise ValueError("The remote candidate history changed before publication. Re-run the workflow.")

    push = subprocess.run(["git", "-c", "push.followTags=false", "push", "origin", f"{tag_object}:{ref}"])
    latest = remote_candidates(series)
    verify_target(remote_target(latest, tag))
    # An identical tag object ID also verifies the remote annotation bytes.
    if push.returncode or latest[tag] != tag_object:
        summarize("Reused concurrently published candidate")
    else:
        summarize("Published and verified annotated candidate")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("detect", "publish"))
    args = parser.parse_args()
    head = commit_id(os.environ["GITHUB_SHA"])
    if git("rev-parse", "HEAD").decode().strip() != head:
        raise ValueError("The checkout does not match the event commit.")
    if args.operation == "detect":
        detect(os.environ["GITHUB_EVENT_NAME"], head)
    else:
        publish(commit_id(os.environ["CANDIDATE_COMMIT"]), commit_id(os.environ["PREVIOUS_COMMIT"]))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Candidate tag: {error}") from None
