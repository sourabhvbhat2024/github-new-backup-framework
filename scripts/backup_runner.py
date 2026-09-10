"""
backup_runner.py

Performs one full backup cycle for the GitHub repository configured in
config/config.yaml:

  1. Clone the repo as a git mirror (full history, all branches and tags).
  2. Extract issues and releases via the GitHub REST API (PRs are
     intentionally excluded -- see config.yaml and dissertation scope notes).
  3. Package everything into a single timestamped archive.
  4. Compute a SHA-256 checksum of the archive.
  5. Upload the archive to S3 (versioned bucket).
  6. Record the backup in the manifest (for later restore) and the log
     (for later metric computation).

Run directly from the command line:
    python backup_runner.py

Or import run_backup() from the Streamlit dashboard / restore script.
"""

import json
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path

import boto3
import requests

from common import (
    append_log_entry,
    append_manifest_entry,
    get_github_token,
    load_config,
    sha256_of_file,
    utc_now_iso,
)

GITHUB_API = "https://api.github.com"


def clone_mirror(repo_owner, repo_name, dest_dir):
    """
    Clone the repository as a mirror -- this captures all branches and tags,
    not just the default branch, which is what makes this suitable as a
    backup rather than a normal working clone.
    """
    repo_url = f"https://github.com/{repo_owner}/{repo_name}.git"
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    result = subprocess.run(
        ["git", "clone", "--mirror", repo_url, str(dest_dir)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone --mirror failed: {result.stderr}")
    return dest_dir


def fetch_paginated(url, headers, params=None):
    """
    GitHub's API paginates results at 100 items per page. This walks every
    page using the Link header until there's no 'next' page left, so large
    repos with hundreds of issues don't get silently truncated.
    """
    results = []
    params = dict(params or {})
    params["per_page"] = 100
    while url:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        results.extend(response.json())
        # After the first request, params are already encoded into the next URL
        params = None
        url = response.links.get("next", {}).get("url")
    return results


def fetch_issues(repo_owner, repo_name, token):
    """
    Fetch all issues (open and closed), including their comments.
    Note: GitHub's REST API returns pull requests inside the issues
    endpoint too (since a PR is technically a special issue). We filter
    those out explicitly, since PR backup/restore is out of scope.
    """
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    url = f"{GITHUB_API}/repos/{repo_owner}/{repo_name}/issues"
    raw_issues = fetch_paginated(url, headers, params={"state": "all"})

    issues = []
    for issue in raw_issues:
        if "pull_request" in issue:
            continue  # skip PRs -- out of scope, see config.yaml
        comments_url = issue.get("comments_url")
        comments = []
        if comments_url and issue.get("comments", 0) > 0:
            comments = fetch_paginated(comments_url, headers)
        issues.append(
            {
                "number": issue["number"],
                "title": issue["title"],
                "body": issue.get("body"),
                "state": issue["state"],
                "labels": [label["name"] for label in issue.get("labels", [])],
                "created_at": issue["created_at"],
                "closed_at": issue.get("closed_at"),
                "comments": [
                    {
                        "author": c["user"]["login"] if c.get("user") else None,
                        "body": c["body"],
                        "created_at": c["created_at"],
                    }
                    for c in comments
                ],
            }
        )
    return issues


def fetch_releases(repo_owner, repo_name, token):
    """Fetch all releases, including their asset metadata (not the binary asset files themselves)."""
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    url = f"{GITHUB_API}/repos/{repo_owner}/{repo_name}/releases"
    raw_releases = fetch_paginated(url, headers)

    releases = []
    for rel in raw_releases:
        releases.append(
            {
                "tag_name": rel["tag_name"],
                "name": rel.get("name"),
                "body": rel.get("body"),
                "draft": rel.get("draft", False),
                "prerelease": rel.get("prerelease", False),
                "created_at": rel.get("created_at"),
                "published_at": rel.get("published_at"),
                "assets": [
                    {"name": a["name"], "size": a["size"], "download_count": a["download_count"]}
                    for a in rel.get("assets", [])
                ],
            }
        )
    return releases


def extract_metadata(config, repo_owner, repo_name):
    """
    Pull together issues and releases per the metadata_types listed in
    config.yaml. Returns a single dict ready to be serialized to JSON.
    """
    token = get_github_token()
    metadata_types = config["backup"].get("metadata_types", [])
    metadata = {}

    if "issues" in metadata_types:
        metadata["issues"] = fetch_issues(repo_owner, repo_name, token)
    if "releases" in metadata_types:
        metadata["releases"] = fetch_releases(repo_owner, repo_name, token)

    return metadata


def package_backup(mirror_dir, metadata, work_dir, backup_id):
    """
    Combine the git mirror and the metadata JSON into a single tar.gz
    archive. Packaging both together means a single S3 object is a
    complete, self-contained backup of one point in time.
    """
    package_dir = work_dir / backup_id
    package_dir.mkdir(parents=True, exist_ok=True)

    # Move the mirrored .git directory in
    shutil.copytree(mirror_dir, package_dir / "repo.git")

    # Write metadata alongside it
    with open(package_dir / "metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)

    archive_path = work_dir / f"{backup_id}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(package_dir, arcname=backup_id)

    shutil.rmtree(package_dir)
    shutil.rmtree(mirror_dir)
    return archive_path


def upload_to_s3(config, archive_path, backup_id):
    """Upload the archive to the configured S3 bucket under a clear, sortable key."""
    s3 = boto3.client("s3", region_name=config["aws"]["region"])
    bucket = config["aws"]["s3_bucket"]
    repo_name = config["github"]["repo_name"]
    key = f"backups/{repo_name}/{backup_id}/repo.tar.gz"
    s3.upload_file(str(archive_path), bucket, key)
    return f"s3://{bucket}/{key}"


def run_backup():
    """
    Run one full backup cycle and return a result dict. This is the function
    both the CLI entry point and the Streamlit 'Run backup now' button call,
    so behavior is identical whether triggered manually or via cron/Actions.
    """
    config = load_config()
    repo_owner = config["github"]["repo_owner"]
    repo_name = config["github"]["repo_name"]
    work_dir = Path(config["paths"]["work_dir"])
    work_dir.mkdir(parents=True, exist_ok=True)

    backup_id = f"{utc_now_iso().replace(':', '-')}_{uuid.uuid4().hex[:8]}"
    started_at = time.time()
    timestamp = utc_now_iso()

    try:
        mirror_dir = work_dir / "mirror_tmp"
        clone_mirror(repo_owner, repo_name, mirror_dir)

        metadata = {}
        if config["backup"].get("include_metadata", True):
            metadata = extract_metadata(config, repo_owner, repo_name)

        archive_path = package_backup(mirror_dir, metadata, work_dir, backup_id)
        checksum = sha256_of_file(archive_path)
        size_bytes = archive_path.stat().st_size

        s3_uri = upload_to_s3(config, archive_path, backup_id)
        archive_path.unlink()  # clean up local copy after successful upload

        duration = round(time.time() - started_at, 2)

        append_manifest_entry(
            config,
            {
                "backup_id": backup_id,
                "timestamp": timestamp,
                "repo": f"{repo_owner}/{repo_name}",
                "s3_uri": s3_uri,
                "checksum_sha256": checksum,
                "size_bytes": size_bytes,
            },
        )
        append_log_entry(
            config,
            {
                "timestamp": timestamp,
                "operation": "backup",
                "status": "success",
                "repo": f"{repo_owner}/{repo_name}",
                "size_bytes": size_bytes,
                "duration_seconds": duration,
                "backup_id": backup_id,
                "error_message": "",
            },
        )
        return {"status": "success", "backup_id": backup_id, "duration_seconds": duration}

    except Exception as e:
        duration = round(time.time() - started_at, 2)
        append_log_entry(
            config,
            {
                "timestamp": timestamp,
                "operation": "backup",
                "status": "failed",
                "repo": f"{repo_owner}/{repo_name}",
                "size_bytes": "",
                "duration_seconds": duration,
                "backup_id": backup_id,
                "error_message": str(e),
            },
        )
        return {"status": "failed", "error": str(e)}


if __name__ == "__main__":
    result = run_backup()
    if result["status"] == "success":
        print(f"Backup succeeded: {result['backup_id']} ({result['duration_seconds']}s)")
        sys.exit(0)
    else:
        print(f"Backup failed: {result['error']}")
        sys.exit(1)
