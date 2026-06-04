import os
import sqlite3
import pandas as pd
import streamlit as st
from contextlib import closing

DB_FILE = "tickets.db"
WORKLOAD_FILE = "workloads.json"  # <-- Hooking up your tracking file

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
st.markdown("Monitor real-time IT routing decisions processed by your background AI Engine.")

# Dashboard stats blocks
df_tickets = load_triaged_tickets()

if not df_tickets.empty:
    total_processed = len(df_tickets)
    actionable_count = len(df_tickets[df_tickets["Actionable?"] == "✅ Yes"])
    unassigned_count = len(df_tickets[df_tickets["Assigned Owner"] == "Unassigned"])

    c1, c2, c3 = st.columns(3)
    c1.metric("Total Tickets Triaged", total_processed)
    c2.metric("Actionable Tickets Routed", actionable_count)
    c3.metric("Failed / Unassigned", unassigned_count)
else:
    st.info("Waiting for the background AI Engine to process some emails... Make sure engine.py is running in your terminal!")

# Refresh and management controls
col1, col2 = st.columns([1, 4])
with col1:
    if st.button("Manual Refresh Data", type="primary", use_container_width=True):
        st.rerun()
with col2:
    if st.button("Wipe DB and Clear History", use_container_width=True):
        clear_ticket_history()
        st.rerun()

st.subheader("📋 Live Routing Registry")
table_placeholder = st.empty()

if not df_tickets.empty:
    table_placeholder.dataframe(
        df_tickets,
        use_container_width=True,
        column_config={
            "Tournament Evaluation Reasoning": st.column_config.TextColumn("Tournament Evaluation Reasoning", width="large")
        }
    )
else:
    table_placeholder.info("No ticket history records found.")