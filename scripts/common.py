"""
common.py

Shared helper functions used by backup_runner.py, restore_runner.py, and the
Streamlit dashboard. Keeping these in one place means the backup script, the
restore script, and the UI all agree on what a "log entry" or "manifest
entry" looks like -- which matters when you're computing Backup Success
Rate, RPO, and RTO later from the same log file.
"""

import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.yaml"


def load_config():
    """Load config.yaml as a plain dict."""
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


def utc_now_iso():
    """Current UTC time as an ISO-8601 string, used consistently for all timestamps."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_of_file(filepath, chunk_size=8192):
    """
    Compute SHA-256 checksum of a file by streaming it in chunks, so this
    works even for backup archives too large to load into memory at once.
    """
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def ensure_log_files_exist(config):
    """Create the log CSV and manifest JSON if they don't exist yet, with headers."""
    log_path = Path(config["paths"]["log_file"])
    manifest_path = Path(config["paths"]["manifest_file"])

    log_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    if not log_path.exists():
        with open(log_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "timestamp",
                    "operation",
                    "status",
                    "repo",
                    "size_bytes",
                    "duration_seconds",
                    "backup_id",
                    "error_message",
                ]
            )

    if not manifest_path.exists():
        with open(manifest_path, "w") as f:
            json.dump([], f)


def append_log_entry(config, entry: dict):
    """
    Append one row to the CSV log. entry should contain keys matching the
    header in ensure_log_files_exist. Missing keys are written as blank.
    This CSV is the raw data source for your evaluation chapter -- every
    backup and restore attempt, success or failure, lands here.
    """
    ensure_log_files_exist(config)
    log_path = Path(config["paths"]["log_file"])
    fieldnames = [
        "timestamp",
        "operation",
        "status",
        "repo",
        "size_bytes",
        "duration_seconds",
        "backup_id",
        "error_message",
    ]
    with open(log_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        row = {k: entry.get(k, "") for k in fieldnames}
        writer.writerow(row)


def append_manifest_entry(config, entry: dict):
    """
    Append one record to manifest.json. The manifest is the source of truth
    for *what backups exist and their checksums* -- separate from the log,
    which records *what happened on each run*. The restore script reads the
    manifest to know which checksum to verify against.
    """
    ensure_log_files_exist(config)
    manifest_path = Path(config["paths"]["manifest_file"])
    with open(manifest_path, "r") as f:
        manifest = json.load(f)
    manifest.append(entry)
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)


def load_manifest(config):
    """Return the full list of manifest entries (one per successful backup)."""
    ensure_log_files_exist(config)
    manifest_path = Path(config["paths"]["manifest_file"])
    with open(manifest_path, "r") as f:
        return json.load(f)


def load_log_entries(config):
    """Return all log rows as a list of dicts, used by the dashboard for metrics."""
    ensure_log_files_exist(config)
    log_path = Path(config["paths"]["log_file"])
    with open(log_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


def get_github_token():
    """
    Read the GitHub Personal Access Token from the environment.
    Never read this from config.yaml -- keeps secrets out of any file you
    might accidentally commit or hand to your supervisor.
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN environment variable is not set. "
            "Run: export GITHUB_TOKEN=<your_personal_access_token>"
        )
    return token
