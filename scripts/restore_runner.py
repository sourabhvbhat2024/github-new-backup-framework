"""
restore_runner.py

Restores a previously created backup to a NEW, empty GitHub repository:

  1. Look up the chosen backup in the manifest to get its S3 location and
     stored checksum.
  2. Download the archive from S3.
  3. Recompute its SHA-256 checksum and compare against the manifest --
     if these don't match, stop immediately. This is the integrity gate:
     a corrupted backup should never silently get pushed live.
  4. Extract the archive and git push --mirror into the target repo.
  5. Replay metadata: recreate issues and releases via the GitHub API.
     (Pull requests are not replayed -- see scope notes in config.yaml.)

Run directly from the command line:
    python restore_runner.py <backup_id> <target_repo_name>

Or import run_restore() from the Streamlit dashboard.
"""

import json
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import boto3
import requests

from common import (
    append_log_entry,
    get_github_token,
    load_config,
    load_manifest,
    sha256_of_file,
    utc_now_iso,
)

GITHUB_API = "https://api.github.com"


class ChecksumMismatchError(Exception):
    """Raised when a downloaded backup's checksum does not match the manifest record."""
    pass


def find_manifest_entry(config, backup_id):
    """Look up a specific backup's manifest record by its backup_id."""
    manifest = load_manifest(config)
    for entry in manifest:
        if entry["backup_id"] == backup_id:
            return entry
    raise ValueError(f"No manifest entry found for backup_id={backup_id}")


def download_from_s3(config, s3_uri, dest_path):
    """Download the backup archive from S3 to a local path."""
    s3 = boto3.client("s3", region_name=config["aws"]["region"])
    # s3_uri looks like s3://bucket/key
    _, _, rest = s3_uri.partition("s3://")
    bucket, _, key = rest.partition("/")
    s3.download_file(bucket, key, str(dest_path))


def verify_checksum(archive_path, expected_checksum):
    """
    Recompute the checksum of the downloaded archive and compare it to the
    value recorded at backup time. This is the single most important check
    in the whole recovery path -- it is what turns 'we have a copy' into
    'we have a trustworthy copy'.
    """
    actual_checksum = sha256_of_file(archive_path)
    if actual_checksum != expected_checksum:
        raise ChecksumMismatchError(
            f"Checksum mismatch: expected {expected_checksum}, got {actual_checksum}. "
            "The backup archive may be corrupted or tampered with. Restore aborted."
        )
    return actual_checksum


def extract_archive(archive_path, work_dir, backup_id):
    """Extract the tar.gz archive, returning paths to the git mirror dir and metadata file."""
    extract_dir = work_dir / f"restore_{backup_id}"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True)

    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(extract_dir)

    mirror_dir = extract_dir / backup_id / "repo.git"
    metadata_path = extract_dir / backup_id / "metadata.json"
    return mirror_dir, metadata_path, extract_dir


def create_target_repo(target_repo_name, token):
    """
    Create a new, empty GitHub repository to restore into. Restoring into a
    brand-new repo (rather than overwriting an existing one) avoids
    accidentally destroying live data during a demo or a real recovery.
    """
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    response = requests.post(
        f"{GITHUB_API}/user/repos",
        headers=headers,
        json={"name": target_repo_name, "private": True, "auto_init": False},
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"Failed to create target repo: {response.status_code} {response.text}")
    return response.json()


def push_mirror(mirror_dir, target_clone_url):
    """Push the extracted mirror into the newly created target repository."""
    result = subprocess.run(
        ["git", "push", "--mirror", target_clone_url],
        cwd=str(mirror_dir),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git push --mirror failed: {result.stderr}")


def replay_issues(issues, repo_owner, target_repo_name, token):
    """
    Recreate each issue as a new issue in the target repo, with its
    comments appended as a single body note (GitHub's API does not let you
    set a comment's original author or timestamp, so we keep that context
    visible in the text instead of pretending the comment is "live").
    """
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    created = 0
    for issue in issues:
        body = issue.get("body") or ""
        if issue.get("comments"):
            body += "\n\n---\n**Restored comments:**\n"
            for c in issue["comments"]:
                body += f"\n> {c['author']} ({c['created_at']}): {c['body']}\n"

        payload = {
            "title": f"[Restored] {issue['title']}",
            "body": body,
            "labels": issue.get("labels", []),
        }
        response = requests.post(
            f"{GITHUB_API}/repos/{repo_owner}/{target_repo_name}/issues",
            headers=headers,
            json=payload,
        )
        if response.status_code == 201:
            created += 1
            if issue.get("state") == "closed":
                issue_number = response.json()["number"]
                requests.patch(
                    f"{GITHUB_API}/repos/{repo_owner}/{target_repo_name}/issues/{issue_number}",
                    headers=headers,
                    json={"state": "closed"},
                )
    return created


def replay_releases(releases, repo_owner, target_repo_name, token):
    """Recreate each release in the target repo. Binary assets are not re-uploaded -- only metadata."""
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    created = 0
    for rel in releases:
        payload = {
            "tag_name": rel["tag_name"],
            "name": rel.get("name"),
            "body": rel.get("body"),
            "draft": rel.get("draft", False),
            "prerelease": rel.get("prerelease", False),
        }
        response = requests.post(
            f"{GITHUB_API}/repos/{repo_owner}/{target_repo_name}/releases",
            headers=headers,
            json=payload,
        )
        if response.status_code == 201:
            created += 1
    return created


def run_restore(backup_id, target_repo_name):
    """
    Run one full restore cycle and return a result dict. Used by both the
    CLI entry point and the Streamlit 'Restore' button.
    """
    config = load_config()
    repo_owner = config["github"]["repo_owner"]
    work_dir = Path(config["paths"]["work_dir"])
    work_dir.mkdir(parents=True, exist_ok=True)

    started_at = time.time()
    timestamp = utc_now_iso()

    try:
        manifest_entry = find_manifest_entry(config, backup_id)
        archive_path = work_dir / f"{backup_id}_download.tar.gz"

        download_from_s3(config, manifest_entry["s3_uri"], archive_path)
        verify_checksum(archive_path, manifest_entry["checksum_sha256"])

        mirror_dir, metadata_path, extract_dir = extract_archive(archive_path, work_dir, backup_id)

        token = get_github_token()
        target_repo = create_target_repo(target_repo_name, token)
        push_mirror(mirror_dir, target_repo["clone_url"].replace(
            "https://", f"https://{token}@"
        ))

        issues_restored = 0
        releases_restored = 0
        if metadata_path.exists():
            with open(metadata_path, "r") as f:
                metadata = json.load(f)
            if "issues" in metadata:
                issues_restored = replay_issues(metadata["issues"], repo_owner, target_repo_name, token)
            if "releases" in metadata:
                releases_restored = replay_releases(metadata["releases"], repo_owner, target_repo_name, token)

        archive_path.unlink()
        shutil.rmtree(extract_dir)

        duration = round(time.time() - started_at, 2)
        append_log_entry(
            config,
            {
                "timestamp": timestamp,
                "operation": "restore",
                "status": "success",
                "repo": target_repo_name,
                "size_bytes": "",
                "duration_seconds": duration,
                "backup_id": backup_id,
                "error_message": "",
            },
        )
        return {
            "status": "success",
            "duration_seconds": duration,
            "issues_restored": issues_restored,
            "releases_restored": releases_restored,
            "target_repo_url": target_repo["html_url"],
        }

    except Exception as e:
        duration = round(time.time() - started_at, 2)
        append_log_entry(
            config,
            {
                "timestamp": timestamp,
                "operation": "restore",
                "status": "failed",
                "repo": target_repo_name,
                "size_bytes": "",
                "duration_seconds": duration,
                "backup_id": backup_id,
                "error_message": str(e),
            },
        )
        return {"status": "failed", "error": str(e)}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python restore_runner.py <backup_id> <target_repo_name>")
        sys.exit(1)

    backup_id_arg = sys.argv[1]
    target_repo_arg = sys.argv[2]
    result = run_restore(backup_id_arg, target_repo_arg)
    if result["status"] == "success":
        print(f"Restore succeeded in {result['duration_seconds']}s -> {result['target_repo_url']}")
        print(f"Issues restored: {result['issues_restored']}, Releases restored: {result['releases_restored']}")
        sys.exit(0)
    else:
        print(f"Restore failed: {result['error']}")
        sys.exit(1)
