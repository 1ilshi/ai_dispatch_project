# engine.py
import base64
import email
import imaplib
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from email.header import decode_header
from typing import List, Dict, Any

import pandas as pd
import requests
from dotenv import load_dotenv
from pandas import DataFrame

# Enterprise Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load configurations
load_dotenv()
BOT_EMAIL = os.getenv("BOT_EMAIL")
BOT_APP_PASSWORD = os.getenv("BOT_APP_PASSWORD")
IMAP_SERVER = os.getenv("IMAP_SERVER")

OLLAMA_VISION_MODEL = "llava-phi3:latest"
OLLAMA_BRAIN_MODEL = "llama3:latest"
OLLAMA_API_URL = "http://localhost:11434/api/generate"

try:
    from prompts import LLAMA3_ROUTING_PROMPT
except ImportError:
    logger.error("Could not import LLAMA3_ROUTING_PROMPT from prompts.py. Ensure prompts.py exists.")
    raise

DB_FILE = "tickets.db"
WORKLOAD_FILE = "workloads.json"

def init_db():
    """Initializes a local SQLite database to store triaged ticket history securely."""
    with closing(sqlite3.connect(DB_FILE)) as conn:
        with conn:
            conn.execute("""
                         CREATE TABLE IF NOT EXISTS triaged_tickets (
                                                                        email_id TEXT PRIMARY KEY,
                                                                        payload_type TEXT,
                                                                        subject TEXT,
                                                                        cc_list TEXT,
                                                                        actionable TEXT,
                                                                        assigned_owner TEXT,
                                                                        target_queue TEXT,
                                                                        responsibility TEXT,
                                                                        evaluation TEXT,
                                                                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                         )
                         """)
    logger.info("Database initialized successfully.")

def load_or_init_workloads(matrix_df: pd.DataFrame) -> dict:
    """Loads workload state from disk, or initializes it based on unique matrix owners."""
    if os.path.exists(WORKLOAD_FILE):
        with open(WORKLOAD_FILE, "r") as f:
            return json.load(f)

    unique_owners = matrix_df['Owner'].unique()
    initial_workloads = {str(owner): 0 for owner in unique_owners if str(owner) != "(no value)"}

    with open(WORKLOAD_FILE, "w") as f:
        json.dump(initial_workloads, f)

    return initial_workloads

def update_workload(owner_name: str):
    """Increments the active ticket count for the assigned owner."""
    if not owner_name or owner_name == "Unassigned":
        return

    if os.path.exists(WORKLOAD_FILE):
        with open(WORKLOAD_FILE, "r") as f:
            workloads = json.load(f)

        if owner_name in workloads:
            workloads[owner_name] += 1
        else:
            workloads[owner_name] = 1

        with open(WORKLOAD_FILE, "w") as f:
            json.dump(workloads, f)

def is_already_processed(email_id: str) -> bool:
    """Checks if this specific email ID has already been triaged by our engine."""
    with closing(sqlite3.connect(DB_FILE)) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM triaged_tickets WHERE email_id = ?", (email_id,))
        return cursor.fetchone() is not None

def save_triaged_ticket(ticket_data: Dict[str, Any]):
    """Saves the final routing decisions into the local database."""
    with closing(sqlite3.connect(DB_FILE)) as conn:
        with conn:
            conn.execute("""
                INSERT OR REPLACE INTO triaged_tickets 
                (email_id, payload_type, subject, cc_list, actionable, assigned_owner, target_queue, responsibility, evaluation)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ticket_data["email_id"],
                ticket_data["payload_type"],
                ticket_data["subject"],
                ticket_data["cc_list"],
                ticket_data["actionable"],
                ticket_data["assigned_owner"],
                ticket_data["target_queue"],
                ticket_data["responsibility"],
                ticket_data["evaluation"]
            ))

def load_staff_matrix_df(filepath: str = "Staff_itrt.xlsx") -> DataFrame | None:
    """Loads the matrix into a pandas DataFrame."""
    try:
        if filepath.endswith('.xlsx'):
            df = pd.read_excel(filepath)
        else:
            df = pd.read_csv(filepath)
        df = df.fillna("(no value)")
        return df
    except Exception as e:
        logger.error(f"Error loading staff matrix: {e}")
        return None

def fetch_unread_emails() -> List[Dict[str, Any]]:
    """Retrieves unread emails from IMAP and filters out already processed items."""
    logger.info(f"Connecting to IMAP server: {IMAP_SERVER}")
    unprocessed_emails = []

    try:
        with closing(imaplib.IMAP4_SSL(IMAP_SERVER)) as mail:
            mail.login(BOT_EMAIL, BOT_APP_PASSWORD)
            mail.select("inbox")

            status, messages = mail.search(None, "UNSEEN")
            email_ids = messages[0].split()

            if not email_ids:
                logger.info("Inbox checked. 0 new unread emails found.")
                return []

            logger.info(f"Found {len(email_ids)} unread emails on server. Filtering for unprocessed...")

            for idx, e_id in enumerate(email_ids, 1):
                email_id_str = e_id.decode("utf-8")

                if is_already_processed(email_id_str):
                    continue

                _, msg_data = mail.fetch(e_id, "(RFC822)")
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])

                        subject, encoding = decode_header(msg.get("Subject", ""))[0]
                        if isinstance(subject, bytes):
                            subject = subject.decode(encoding if encoding else "utf-8")

                        cc_header = msg.get("Cc", "")
                        cc_list = ""
                        if cc_header:
                            cc, encoding = decode_header(cc_header)[0]
                            if isinstance(cc, bytes):
                                cc_list = cc.decode(encoding if encoding else "utf-8")
                            else:
                                cc_list = str(cc)

                        body = ""
                        images_b64 = []

                        if msg.is_multipart():
                            for part in msg.walk():
                                content_type = part.get_content_type()
                                if content_type == "text/plain":
                                    try:
                                        body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                                    except Exception as err:
                                        logger.warning(f"Failed to decode text payload: {err}")
                                elif part.get_content_maintype() == "image":
                                    img_data = part.get_payload(decode=True)
                                    if img_data:
                                        b64_img = base64.b64encode(img_data).decode("utf-8")
                                        images_b64.append(b64_img)
                                        logger.info(f"Extracted attached screenshot.")
                        else:
                            body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

                        logger.info(f"[{idx}/{len(email_ids)}] Queueing for analysis: '{str(subject)[:40]}...'")
                        unprocessed_emails.append({
                            "email_id": email_id_str,
                            "subject": subject if subject else "(No Subject Provided)",
                            "cc": cc_list,
                            "body": body[:1500],
                            "images": images_b64
                        })
    except Exception as e:
        logger.error(f"IMAP Connection or Fetching Failure: {e}")

    return unprocessed_emails

def process_images_with_llava(base64_images: List[str]) -> str:
    """STAGE 1: Uses Llava-Phi3 to extract UI context and error logs from attachments."""
    if not base64_images:
        return "No visual evidence attached."

    logger.info(f"Sending {len(base64_images)} image(s) to Llava-Phi3...")
    prompt = (
        "Analyze this IT support image. It could be a clean desktop screenshot OR a phone camera photo "
        "of a physical screen, device, or hardware environment. Provide your analysis in exactly two sections:\n\n"
        "1. TEXT EXTRACTED: Extract all legible text, error codes, system messages, or button labels word-for-word. "
        "If no text is visible, write 'None'.\n"
        "2. PHOTO CONTEXT & DESCRIPTION: Describe what the photo physically depicts. Explain what is happening "
        "in the scene."
    )

    payload = {"model": OLLAMA_VISION_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.0},
               "images": base64_images}

    try:
        response = requests.post(OLLAMA_API_URL, json=payload, timeout=180)
        response.raise_for_status()
        return response.json()["response"].strip()
    except Exception as e:
        logger.error(f"Llava-Phi3 Vision processing failed: {e}")
        return f"Failed to analyze image assets: {str(e)}"

def analyze_email_with_llama3(subject: str, cc_list: str, body: str, vision_description: str, markdown_matrix: str) -> Dict[str, Any]:
    """STAGE 2: Semantic Analysis and Identification of Candidate Pool via Llama 3."""
    logger.info(f"Passing processed dimensions to Llama 3...")

    try:
        safe_subject = f"```\n{subject}\n```"
        safe_cc = f"```\n{cc_list if cc_list else 'None'}\n```"
        safe_body = f"```\n{body}\n```"
        safe_vision = f"```\n{vision_description}\n```"

        prompt = LLAMA3_ROUTING_PROMPT.format(
            markdown_matrix=markdown_matrix,
            subject=safe_subject,
            cc_list=safe_cc,
            body=safe_body,
            vision_description=safe_vision
        )

        payload = {
            "model": OLLAMA_BRAIN_MODEL,
            "prompt": prompt,
            "format": "json",
            "stream": False,
            "options": {
                "temperature": 0.0,
                "top_p": 0.1
            }
        }

        response = requests.post(OLLAMA_API_URL, json=payload, timeout=180)
        response.raise_for_status()
        raw_text = response.json()["response"]

        return json.loads(raw_text.strip())

    except Exception as e:
        logger.error(f"Llama 3 logical pipeline crash: {e}")
        return {
            "is_valid_request": False,
            "queue": "Error/Unassigned",
            "responsibility": "Error Recovery",
            "candidate_pool": []
        }

def run_dispatch_cycle():
    """Runs a single iteration of the routing workflow."""
    logger.info("Starting dispatch cycle...")
    matrix_df = load_staff_matrix_df("Staff_itrt.xlsx")

    if matrix_df is None:
        logger.error("Unable to load matrix data. Aborting this cycle.")
        return

    markdown_matrix = matrix_df.to_markdown(index=False)
    current_workloads = load_or_init_workloads(matrix_df)

    unprocessed = fetch_unread_emails()
    if not unprocessed:
        logger.info("No new emails to process. Cycle complete.")
        return

    logger.info(f"Starting pipeline processing for {len(unprocessed)} new tickets...")
    for idx, em in enumerate(unprocessed, 1):
        logger.info(f"Processing ticket [{idx}/{len(unprocessed)}]: '{em['subject'][:30]}'")

        # Step 1: Semantic analysis and feature extraction from images
        vision_insights = process_images_with_llava(em["images"])

        # Step 2: Route with Llama 3 to get the targeted responsibility and valid candidate pool
        analysis = analyze_email_with_llama3(
            em["subject"], em["cc"], em["body"], vision_insights, markdown_matrix
        )

        # =====================================================================
        # STAGE 3: DETERMINISTIC PYTHON TOURNAMENT (NO LLM MATH HALLUCINATIONS)
        # =====================================================================
        assigned_owner = "Unassigned"
        tournament_logs = "N/A"

        if analysis.get("is_valid_request"):
            candidate_pool = analysis.get("candidate_pool", [])

            # Filter and sanitize strings in the returned candidate list
            valid_candidates = [str(c).strip() for c in candidate_pool if str(c).strip() not in ["", "Unassigned", "N/A"]]

            if valid_candidates:
                # Log out the current active counts of the matching candidates for debugging/UI auditing
                score_cards = [f"{name} ({current_workloads.get(name, 0)} active)" for name in valid_candidates]
                tournament_logs = f"Eligible Candidates Identified: {', '.join(score_cards)}. "

                # Deterministically look up the minimum score across the identified candidate pool
                assigned_owner = min(valid_candidates, key=lambda name: current_workloads.get(name, 0))
                tournament_logs += f"🏆 Winner chosen by Python Engine: {assigned_owner}."
            else:
                tournament_logs = "No valid candidates returned in pool by Llama 3."
        else:
            tournament_logs = "Ticket skipped: Request was flagged as invalid or un-actionable."

        # Build ledger metrics payload for Streamlit UI transparency
        facts_list = analysis.get('technical_facts_extracted', [])
        formatted_facts = "\n".join([f"• {f}" for f in facts_list]) if isinstance(facts_list, list) else f"• {facts_list}"

        ai_thinking = (
            f"📋 **Extracted Facts:**\n{formatted_facts}\n\n"
            f"🎯 **Core Intent:** {analysis.get('core_technical_intent', 'N/A')}\n\n"
            f"📌 **Verbatim Responsibility Match:** {analysis.get('exact_responsibility_match', 'N/A')}\n\n"
            f"🛡️ **Firewall Check:** {analysis.get('firewall_cross_examination', 'N/A')}\n\n"
            f"⚖️ **Python Workload Tournament Result:**\n`{tournament_logs}`"
        )

        # Build final database entry packet
        ticket_packet = {
            "email_id": em["email_id"],
            "payload_type": "🖼️ Image+Text" if em["images"] else "📝 Pure Text",
            "subject": em["subject"],
            "cc_list": em["cc"] if em["cc"] else "-",
            "actionable": "✅ Yes" if analysis.get("is_valid_request") else "❌ No",
            "assigned_owner": assigned_owner,
            "target_queue": analysis.get("queue", "Unassigned"),
            "responsibility": analysis.get("responsibility", "Unassigned"),
            "evaluation": ai_thinking
        }

        # Step 4: Save to SQLite instantly
        save_triaged_ticket(ticket_packet)

        # Step 5: Safely increment workload ledger states if valid request
        if analysis.get("is_valid_request") and assigned_owner != "Unassigned":
            update_workload(assigned_owner)
            current_workloads[assigned_owner] += 1

        logger.info(f"Ticket '{em['subject'][:30]}' triaged and saved: Owner -> {ticket_packet['assigned_owner']}")

if __name__ == "__main__":
    init_db()
    logger.info("Background Engine is running. Press Ctrl+C in this terminal at any time to safely shut down.")

    try:
        while True:
            run_dispatch_cycle()
            logger.info("Sleeping for 60 seconds before next inbox poll...")
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Ctrl+C detected. Gracefully shutting down dispatcher engine. Connections closed.")