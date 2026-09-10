# GitHub backup and recovery framework

An instrumented backup and recovery framework for GitHub repositories,
built as part of an M.Tech dissertation project. Backs up Git history
(all branches and tags) plus issue and release metadata to a versioned
AWS S3 bucket, with checksum-based integrity verification before any
restore.

## Scope

**Backed up and restored:**
- Full Git history (all branches, tags) via `git clone --mirror`
- Issues (including comments)
- Releases (including asset metadata, not binary asset files)

**Explicitly out of scope:**
- Pull requests and their review history. Merged or closed PRs cannot be
  faithfully reconstructed as live, reviewable objects via the GitHub
  API -- restoring them would only produce static, informational copies,
  not functioning PRs. This is a deliberate, fidelity-driven scoping
  decision, not an oversight, and is listed as future work.

## Architecture

```
GitHub repo --> Backup orchestrator --> AWS S3 (versioned)
                  |                         |
                  |-- git mirror            |-- checksum manifest
                  |-- metadata (issues,     |-- backup archives
                      releases)
                                             v
                                     Recovery runner
                                  (checksum verify -> mirror
                                   push -> metadata replay)
                                             |
                                             v
                                     New GitHub repo
```

A Streamlit dashboard sits on top of `backup_runner.py` and
`restore_runner.py` for configuration display, one-click backup/restore,
and live metrics. The dashboard is a thin layer -- it calls the same
functions available from the command line, so the underlying system
works (and can be demoed) independently of the UI.

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Set required environment variables:
   ```
   export GITHUB_TOKEN=<your_personal_access_token>
   export AWS_ACCESS_KEY_ID=<your_key>
   export AWS_SECRET_ACCESS_KEY=<your_secret>
   ```
   The GitHub token needs `repo` scope. AWS credentials need
   `s3:PutObject`, `s3:GetObject`, and `s3:ListBucket` on the target
   bucket (least-privilege -- avoid using root account keys).

3. Edit `config/config.yaml` with your repository and S3 bucket details.

4. Create the S3 bucket with versioning enabled (one-time, via AWS
   Console or CLI):
   ```
   aws s3api create-bucket --bucket <your-bucket-name> --region <your-region>
   aws s3api put-bucket-versioning --bucket <your-bucket-name> --versioning-configuration Status=Enabled
   ```

## Usage

**Run a backup:**
```
python scripts/backup_runner.py
```

**Restore a backup to a new repository:**
```
python scripts/restore_runner.py <backup_id> <target_repo_name>
```
(`backup_id` values are listed in `logs/manifest.json` after at least
one successful backup.)

**Launch the dashboard:**
```
streamlit run app.py
```

## Evaluation metrics

The dashboard computes these directly from `logs/backup_log.csv`:

- **Backup Success Rate** -- successful backup runs / total backup runs
- **RPO (observed)** -- average time gap between consecutive successful backups
- **RTO** -- average duration of successful restore operations
- **Data integrity** -- enforced structurally: a restore can only
  proceed if the downloaded archive's SHA-256 checksum matches the
  value recorded in the manifest at backup time

For the dissertation's testing chapter, recommended failure-mode tests:
- Delete a test repository entirely, then restore it from S3, and time
  the full recovery.
- Manually corrupt a stored archive (flip a byte) and confirm
  `restore_runner.py` raises `ChecksumMismatchError` and aborts before
  any data is pushed.

## File structure

```
github-backup-framework/
├── app.py                   # Streamlit dashboard
├── requirements.txt
├── config/
│   └── config.yaml          # repo, bucket, and scope settings
├── scripts/
│   ├── common.py             # shared config/logging/checksum helpers
│   ├── backup_runner.py      # backup logic
│   └── restore_runner.py     # restore logic
└── logs/
    ├── backup_log.csv        # append-only run history (for metrics)
    └── manifest.json         # backup_id -> S3 location + checksum
```
