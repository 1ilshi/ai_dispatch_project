import os
import json
import sqlite3
import pandas as pd
import streamlit as st
import plotly.express as px
from contextlib import closing

DB_FILE = "tickets.db"
WORKLOAD_FILE = "workloads.json"

def load_triaged_tickets():
    """Reads all historical ticket data stored in SQLite by our background engine."""
    if not os.path.exists(DB_FILE):
        return pd.DataFrame()

    try:
        with closing(sqlite3.connect(DB_FILE)) as conn:
            # UPDATED: Added email_date to the SELECT statement and sorting by it
            df = pd.read_sql_query(
                "SELECT email_date, payload_type, ticket_type, subject, cc_list, actionable, assigned_owner, target_queue, responsibility, evaluation, timestamp FROM triaged_tickets ORDER BY email_date DESC",
                conn
            )
            # UPDATED: Added Email Arrived At to the column mapping
            df.columns = [
                "Email Arrived At", "Payload Type", "Ticket Type", "Subject Line", "CC Field Data", "Actionable?",
                "Assigned Owner", "Target Queue", "Responsibility",
                "Tournament Evaluation Reasoning", "Processed At"
            ]
            return df
    except Exception as e:
        # If the column doesn't exist yet, it means the user hasn't wiped the DB.
        st.sidebar.error("Database Schema Mismatch! Please click 'Wipe Database' below.")
        return pd.DataFrame()

def load_live_workloads():
    """Reads the current active workload tracking counts from disk."""
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
    if os.path.exists(DB_FILE):
        try:
            with closing(sqlite3.connect(DB_FILE)) as conn:
                with conn:
                    conn.execute("DROP TABLE IF EXISTS triaged_tickets")
            st.sidebar.success("Database wiped clean!")
        except Exception as e:
            st.sidebar.error(f"Error clearing database: {e}")

    if os.path.exists(WORKLOAD_FILE):
        try:
            os.remove(WORKLOAD_FILE)
        except Exception as e:
            st.sidebar.error(f"Error deleting workload file: {e}")

# --- Streamlit UI Configurations ---
st.set_page_config(page_title="AI IT Dispatcher", layout="wide", initial_sidebar_state="expanded")

st.markdown("""
    <style>
    .stDataFrame { border-radius: 8px;}
    </style>
""", unsafe_allow_html=True)

# Load backend datasets once per rerun
df_tickets = load_triaged_tickets()
workloads_dict = load_live_workloads()

# -------------------------------------------------------------------------
# SIDEBAR NAVIGATION & CONTROLS
# -------------------------------------------------------------------------
st.sidebar.title("🤖 AI Dispatcher")
st.sidebar.markdown("Autonomous Routing Console")
st.sidebar.write("---")

# The Menu
menu = st.sidebar.radio(
    "Navigation Menu",
    ["📋 Tickets Table", "📊 Team Workload", "📈 Analysis & Insights"]
)

st.sidebar.write("---")
st.sidebar.markdown("**System Controls**")

if st.sidebar.button("🔄 Refresh Data", use_container_width=True):
    st.rerun()

if st.sidebar.button("🗑️ Wipe Database", type="primary", use_container_width=True):
    clear_ticket_history()
    st.rerun()

# -------------------------------------------------------------------------
# 📥 DATA EXPORT MODULE (NEW)
# -------------------------------------------------------------------------
st.sidebar.write("---")
st.sidebar.markdown("**📥 Data Export**")

if not df_tickets.empty and "Email Arrived At" in df_tickets.columns:
    # Convert string dates to pandas datetime objects for filtering
    df_tickets['Email Arrived At'] = pd.to_datetime(df_tickets['Email Arrived At'], errors='coerce')

    valid_dates = df_tickets['Email Arrived At'].dropna()

    if not valid_dates.empty:
        min_date = valid_dates.min().date()
        max_date = valid_dates.max().date()

        # Render the Date Range Picker
        date_range = st.sidebar.date_input(
            "Select Date Range",
            value=(min_date, max_date),
            min_value=min_date,
            max_value=max_date
        )

        # Ensure the user has selected both a start and end date
        if len(date_range) == 2:
            start_date, end_date = date_range

            # Filter the dataframe
            mask = (df_tickets['Email Arrived At'].dt.date >= start_date) & (df_tickets['Email Arrived At'].dt.date <= end_date)
            filtered_df = df_tickets.loc[mask]

            # Convert to CSV format
            csv_data = filtered_df.to_csv(index=False).encode('utf-8')

            # Render the Download Button
            st.sidebar.download_button(
                label=f"⬇️ Download {len(filtered_df)} Tickets",
                data=csv_data,
                file_name=f"AIT_Tickets_{start_date}_to_{end_date}.csv",
                mime="text/csv",
                type="primary",
                use_container_width=True
            )
else:
    st.sidebar.info("No data available to export yet.")

# -------------------------------------------------------------------------
# MENU 1: TICKETS TABLE (Simplified)
# -------------------------------------------------------------------------
if menu == "📋 Tickets Table":
    st.title("Live Routing Registry")
    st.markdown("All processed tickets and their assigned routing.")

    if not df_tickets.empty:
        # UPDATED: Added "Email Arrived At" to the front of the table
        display_cols = ["Email Arrived At", "Processed At", "Assigned Owner", "Ticket Type", "Responsibility", "Subject Line", "Target Queue"]
        valid_display_cols = [col for col in display_cols if col in df_tickets.columns]

        # Display the simple table
        st.dataframe(
            df_tickets[valid_display_cols],
            use_container_width=True,
            hide_index=True
        )
    else:
        st.info("No ticket history records found. Ensure engine.py is running.")

# -------------------------------------------------------------------------
# MENU 2: TEAM WORKLOAD
# -------------------------------------------------------------------------
elif menu == "📊 Team Workload":
    st.title("Team Workload Ledger")
    st.markdown("Live tracker of active tickets assigned to each IT staff member.")

    if workloads_dict:
        # Convert dictionary to dataframe for nice charting
        df_workload = pd.DataFrame(list(workloads_dict.items()), columns=['Staff Member', 'Active Tickets'])
        df_workload = df_workload.sort_values(by='Active Tickets', ascending=True)

        col1, col2 = st.columns([1, 2])

        with col1:
            st.dataframe(df_workload, hide_index=True, use_container_width=True)

        with col2:
            fig = px.bar(
                df_workload,
                x='Active Tickets',
                y='Staff Member',
                orientation='h',
                title="Current Ticket Distribution",
                color='Active Tickets',
                color_continuous_scale="Reds"
            )
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No workloads tracked yet.")

# -------------------------------------------------------------------------
# MENU 3: ANALYSIS & INSIGHTS
# -------------------------------------------------------------------------
elif menu == "📈 Analysis & Insights":
    st.title("AI System Analytics")
    st.markdown("Insights into ticket volumes, AI categorization, and queue distribution.")

    if not df_tickets.empty:
        # Top-level metrics
        total_processed = len(df_tickets)
        actionable_count = len(df_tickets[df_tickets["Actionable?"] == "✅ Yes"])
        spam_count = len(df_tickets[df_tickets["Actionable?"] == "❌ No"])

        c1, c2, c3 = st.columns(3)
        c1.metric("Total Emails Processed", total_processed)
        c2.metric("Valid IT Requests", actionable_count)
        c3.metric("Spam / Automated Rejected", spam_count)

        st.write("---")

        col_chart1, col_chart2 = st.columns(2)

        with col_chart1:
            # Donut chart for Ticket Types
            type_counts = df_tickets["Ticket Type"].value_counts().reset_index()
            type_counts.columns = ["Ticket Type", "Count"]
            fig_type = px.pie(
                type_counts,
                names="Ticket Type",
                values="Count",
                hole=0.4,
                title="Distribution of Ticket Types"
            )
            st.plotly_chart(fig_type, use_container_width=True)

        with col_chart2:
            # Bar chart for Target Queues
            queue_counts = df_tickets[df_tickets["Target Queue"] != "Unassigned"]["Target Queue"].value_counts().reset_index()
            queue_counts.columns = ["Target Queue", "Count"]
            fig_queue = px.bar(
                queue_counts,
                x="Target Queue",
                y="Count",
                title="Tickets by Department Queue",
                color="Target Queue"
            )
            st.plotly_chart(fig_queue, use_container_width=True)
    else:
        st.info("Not enough data to generate analytics. Process some tickets first!")