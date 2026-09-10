"""
app.py

Streamlit dashboard for the GitHub Backup and Recovery Framework.

This is a thin UI layer over backup_runner.py and restore_runner.py --
it does not duplicate their logic. Buttons here call the same
run_backup() / run_restore() functions you can also call directly from
the command line, so the system still works (and can be demoed) even if
Streamlit itself is unavailable.

Run with:
    streamlit run app.py
"""

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

from backup_runner import run_backup
from common import load_config, load_log_entries, load_manifest
from restore_runner import run_restore

st.set_page_config(page_title="GitHub Backup and Recovery", layout="wide")

st.title("GitHub backup and recovery dashboard")

config = load_config()

# ---------------------------------------------------------------------------
# Configuration panel
# ---------------------------------------------------------------------------
st.subheader("Backup configuration")
col1, col2 = st.columns(2)
with col1:
    st.text_input("Repository", value=f"{config['github']['repo_owner']}/{config['github']['repo_name']}", disabled=True)
    st.text_input("S3 bucket", value=config["aws"]["s3_bucket"], disabled=True)
with col2:
    st.text_input("AWS region", value=config["aws"]["region"], disabled=True)
    metadata_scope = ", ".join(config["backup"].get("metadata_types", []))
    st.text_input("Metadata scope", value=metadata_scope, disabled=True)

st.caption(
    "Edit config/config.yaml to change these values. "
    "Pull requests are intentionally excluded from metadata scope -- "
    "see the dissertation scope notes in config.yaml for why."
)

if st.button("Run backup now", type="primary"):
    with st.spinner("Running backup..."):
        result = run_backup()
    if result["status"] == "success":
        st.success(f"Backup succeeded: {result['backup_id']} ({result['duration_seconds']}s)")
    else:
        st.error(f"Backup failed: {result['error']}")
    st.rerun()

st.divider()

# ---------------------------------------------------------------------------
# Metrics panel
# ---------------------------------------------------------------------------
st.subheader("Metrics")

log_entries = load_log_entries(config)
backup_entries = [e for e in log_entries if e["operation"] == "backup"]
restore_entries = [e for e in log_entries if e["operation"] == "restore"]

if backup_entries:
    success_count = sum(1 for e in backup_entries if e["status"] == "success")
    success_rate = round(100 * success_count / len(backup_entries), 1)
else:
    success_rate = None

if len(backup_entries) >= 2:
    timestamps = sorted(pd.to_datetime(e["timestamp"]) for e in backup_entries)
    gaps_hours = [
        (timestamps[i] - timestamps[i - 1]).total_seconds() / 3600
        for i in range(1, len(timestamps))
    ]
    avg_rpo_hours = round(sum(gaps_hours) / len(gaps_hours), 1)
else:
    avg_rpo_hours = None

successful_restores = [e for e in restore_entries if e["status"] == "success"]
if successful_restores:
    avg_rto_seconds = round(
        sum(float(e["duration_seconds"]) for e in successful_restores) / len(successful_restores), 1
    )
else:
    avg_rto_seconds = None

if backup_entries:
    last_backup_time = max(pd.to_datetime(e["timestamp"]) for e in backup_entries)
    last_backup_display = last_backup_time.strftime("%Y-%m-%d %H:%M UTC")
else:
    last_backup_display = "No backups yet"

m1, m2, m3, m4 = st.columns(4)
m1.metric("Backup success rate", f"{success_rate}%" if success_rate is not None else "N/A")
m2.metric("Avg RPO (observed)", f"{avg_rpo_hours} h" if avg_rpo_hours is not None else "N/A")
m3.metric("Avg RTO", f"{avg_rto_seconds} s" if avg_rto_seconds is not None else "N/A")
m4.metric("Last backup", last_backup_display)

st.divider()

# ---------------------------------------------------------------------------
# Backup history table
# ---------------------------------------------------------------------------
st.subheader("Backup history")

if backup_entries:
    df = pd.DataFrame(backup_entries)
    df = df[["timestamp", "status", "size_bytes", "duration_seconds", "backup_id", "error_message"]]
    df = df.sort_values("timestamp", ascending=False)
    st.dataframe(df, use_container_width=True, hide_index=True)
else:
    st.info("No backups recorded yet. Run a backup to populate this table.")

st.divider()

# ---------------------------------------------------------------------------
# Recovery panel
# ---------------------------------------------------------------------------
st.subheader("Recovery")

manifest = load_manifest(config)

if manifest:
    options = {
        f"{e['backup_id']} ({e['timestamp']}, {round(e['size_bytes'] / 1024 / 1024, 1)} MB)": e["backup_id"]
        for e in sorted(manifest, key=lambda x: x["timestamp"], reverse=True)
    }
    selected_label = st.selectbox("Select backup", options.keys())
    selected_backup_id = options[selected_label]
    target_repo_name = st.text_input("Target repository name", value="restored-repo")

    st.caption("Checksum will be verified against the manifest before any data is pushed.")

    if st.button("Restore"):
        with st.spinner("Verifying checksum and restoring..."):
            result = run_restore(selected_backup_id, target_repo_name)
        if result["status"] == "success":
            st.success(
                f"Restore succeeded in {result['duration_seconds']}s. "
                f"Issues restored: {result['issues_restored']}, "
                f"Releases restored: {result['releases_restored']}."
            )
            st.markdown(f"[Open restored repository]({result['target_repo_url']})")
        else:
            st.error(f"Restore failed: {result['error']}")
else:
    st.info("No backups available to restore yet.")
