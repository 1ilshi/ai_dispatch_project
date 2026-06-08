import os
import json
import sqlite3
import pandas as pd
import streamlit as st
from contextlib import closing

DB_FILE = "tickets.db"
WORKLOAD_FILE = "workloads.json"  # <-- Hooked up your live tracking file!

def load_triaged_tickets():
    """Reads all historical ticket data stored in SQLite by our background engine."""
    if not os.path.exists(DB_FILE):
        return pd.DataFrame()

    try:
        with closing(sqlite3.connect(DB_FILE)) as conn:
            # Load the database rows ordered by the latest incoming emails first
            df = pd.read_sql_query(
                "SELECT payload_type, subject, cc_list, actionable, assigned_owner, target_queue, responsibility, evaluation, timestamp FROM triaged_tickets ORDER BY timestamp DESC",
                conn
            )
            # Rename database columns to match your original clean dashboard aesthetics
            df.columns = [
                "Payload Type", "Subject Line", "CC Field Data", "Actionable?",
                "Assigned Owner", "Target Queue", "Responsibility",
                "Tournament Evaluation Reasoning", "Processed At"
            ]
            return df
    except Exception as e:
        st.error(f"Database Read Error: {e}")
        return pd.DataFrame()

def load_live_workloads():
    """Reads the current active workload tracking counts from disk for team visibility."""
    if not os.path.exists(WORKLOAD_FILE):
        return {}
    try:
        with open(WORKLOAD_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        st.error(f"Workload Read Error: {e}")
        return {}

def clear_ticket_history():
    """Wipes the database cleanly and deletes the workload tracking ledger."""
    # 1. Clear database entries
    if os.path.exists(DB_FILE):
        try:
            with closing(sqlite3.connect(DB_FILE)) as conn:
                with conn:
                    conn.execute("DELETE FROM triaged_tickets")
            st.success("Triage database wiped clean!")
        except Exception as e:
            st.error(f"Error clearing database: {e}")

    # 2. Obliterate the workload tracker so it resets completely to zero on next ticket
    if os.path.exists(WORKLOAD_FILE):
        try:
            os.remove(WORKLOAD_FILE)
            st.toast("Workload tracking file successfully reset to zero!", icon="🔄")
        except Exception as e:
            st.error(f"Error deleting workload file: {e}")

# --- Streamlit UI Configurations ---
st.set_page_config(page_title="Real-Time IT AI Dispatcher Dashboard", layout="wide")
st.title("🤖 Orchestrated IT Dispatcher Dashboard")
st.markdown("Monitor real-time IT routing decisions and workload balances processed by your background AI Engine.")

# Load backend datasets
df_tickets = load_triaged_tickets()
workloads_dict = load_live_workloads()

# -------------------------------------------------------------------------
# STAGE 1: GLOBAL METRICS BLOCK
# -------------------------------------------------------------------------
if not df_tickets.empty:
    total_processed = len(df_tickets)
    actionable_count = len(df_tickets[df_tickets["Actionable?"] == "✅ Yes"])
    unassigned_count = len(df_tickets[df_tickets["Assigned Owner"] == "Unassigned"])

    c1, c2, c3 = st.columns(3)
    c1.metric("Total Tickets Triaged", total_processed)
    c2.metric("Actionable Tickets Routed", actionable_count)
    c3.metric("Failed / Unassigned / Spammed", unassigned_count)
else:
    st.info("Waiting for the background AI Engine to process some emails... Make sure engine.py is running in your terminal!")

# -------------------------------------------------------------------------
# STAGE 2: LIVE TEAM WORKLOAD LEDGER (NEW PROTOCOL ADDITION)
# -------------------------------------------------------------------------
st.write("---")
st.subheader("📊 Live IT Staff Workload Status")

if workloads_dict:
    # Convert workload dictionary into a clean sorted dataframe for rendering
    df_workloads = pd.DataFrame(
        list(workloads_dict.items()),
        columns=["IT Staff Member", "Active Ticket Count"]
    ).sort_values(by="Active Ticket Count", ascending=True)

    # Split display into a beautiful side-by-side metric grid and bar chart visualization
    wl_col1, wl_col2 = st.columns([2, 3])

    with wl_col1:
        st.markdown("**Current Ticket Distribution Matrix:**")
        # Display individual staff counts in neat columns
        sub_cols = st.columns(min(len(df_workloads), 4))
        for idx, row in enumerate(df_workloads.itertuples()):
            col_target = sub_cols[idx % len(sub_cols)]
            col_target.metric(label=f"👤 {row._1}", value=f"{row._2} Active")

    with wl_col2:
        # Show a clean horizontal bar chart displaying who is handling the heavy lifting
        st.bar_chart(
            df_workloads,
            x="IT Staff Member",
            y="Active Ticket Count",
            color="#4CAF50",
            use_container_width=True
        )
else:
    st.caption("No team workloads tracked yet. The workload ledger will automatically generate when the first valid ticket is routed.")

# -------------------------------------------------------------------------
# STAGE 3: DASHBOARD CONTROLS
# -------------------------------------------------------------------------
st.write("---")
col1, col2 = st.columns([1, 4])
with col1:
    if st.button("Manual Refresh Data", type="primary", use_container_width=True):
        st.rerun()
with col2:
    if st.button("Wipe DB and Clear History", use_container_width=True):
        clear_ticket_history()
        st.rerun()

# -------------------------------------------------------------------------
# STAGE 4: LIVE DISPATCH REGISTRY
# -------------------------------------------------------------------------
st.subheader("📋 Live Routing Registry")
table_placeholder = st.empty()

if not df_tickets.empty:
    table_placeholder.dataframe(
        df_tickets,
        use_container_width=True,
        column_config={
            "Tournament Evaluation Reasoning": st.column_config.TextColumn(
                "Tournament Evaluation Reasoning",
                width="large"
            )
        }
    )
else:
    table_placeholder.info("No ticket history records found.")