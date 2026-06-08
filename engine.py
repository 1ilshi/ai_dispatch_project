# engine.py
import base64
import email
import imaplib
import json
import logging
import os
import random
import sqlite3
import time
import re
from contextlib import closing
from email.header import decode_header
from typing import List, Dict, Any

import pandas as pd
import requests
from dotenv import load_dotenv

# Enterprise Logging Setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load configurations
load_dotenv()
BOT_EMAIL = os.getenv("BOT_EMAIL")
BOT_APP_PASSWORD = os.getenv("BOT_APP_PASSWORD")
IMAP_SERVER = os.getenv("IMAP_SERVER")

# THE NEW ULTRA-FAST 2-MODEL STACK
OLLAMA_VISION_MODEL = "llava-phi3:latest"
OLLAMA_STAGE1_MODEL = "deepseek-r1:1.5b"  # Gatekeeper
OLLAMA_STAGE2_MODEL = "llama3.1:latest"    # Skill Router
OLLAMA_API_URL = "http://localhost:11434/api/generate"

from prompts import STAGE1_GATEKEEPER_PROMPT, STAGE2_SKILL_ROUTER_PROMPT

DB_FILE = "tickets.db"
WORKLOAD_FILE = "workloads.json"
MATRIX_FILE = "IT_Routing_Matrix_sorted.xlsx"

def init_db():
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

def load_relational_maps(filepath: str = MATRIX_FILE) -> tuple:
    """Loads the Excel sheets and returns Python dictionaries for instant O(1) lookups."""
    try:
        skills_df = pd.read_excel(filepath, sheet_name="Skill_Dictionary")
        queue_df = pd.read_excel(filepath, sheet_name="Queue_Map")
        roster_df = pd.read_excel(filepath, sheet_name="Staff_Roster")

        # 1. Markdown string of skills for Mistral to read
        skills_markdown = skills_df.to_markdown(index=False)

        # 2. Map: Unique_Skill -> Queue (e.g., 'SIS' -> 'Application')
        skill_to_queue = dict(zip(queue_df['Unique_Skill'], queue_df['Queue']))

        # 3. Map: Unique_Skill -> List of Owners (e.g., 'SIS' -> ['Supalak', 'Tony'])
        skill_to_owners = roster_df.groupby('Unique_Skill')['Owner'].apply(lambda x: list(x.astype(str))).to_dict()

        return skills_markdown, skill_to_queue, skill_to_owners
    except Exception as e:
        logger.error(f"Error loading relational maps: {e}")
        return None, {}, {}

def load_or_init_workloads(filepath: str = MATRIX_FILE) -> dict:
    if os.path.exists(WORKLOAD_FILE):
        with open(WORKLOAD_FILE, "r") as f:
            return json.load(f)
    try:
        roster_df = pd.read_excel(filepath, sheet_name="Staff_Roster")
        unique_owners = roster_df['Owner'].dropna().unique()
        initial_workloads = {str(owner).strip(): 0 for owner in unique_owners if str(owner).strip() != "(no value)"}
    except Exception as e:
        initial_workloads = {}
    with open(WORKLOAD_FILE, "w") as f:
        json.dump(initial_workloads, f)
    return initial_workloads

def update_workload(owner_name: str):
    if not owner_name or owner_name == "Unassigned":
        return
    if os.path.exists(WORKLOAD_FILE):
        with open(WORKLOAD_FILE, "r") as f:
            workloads = json.load(f)
        workloads[owner_name] = workloads.get(owner_name, 0) + 1
        with open(WORKLOAD_FILE, "w") as f:
            json.dump(workloads, f)

def is_already_processed(email_id: str) -> bool:
    with closing(sqlite3.connect(DB_FILE)) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM triaged_tickets WHERE email_id = ?", (email_id,))
        return cursor.fetchone() is not None

def save_triaged_ticket(ticket_data: Dict[str, Any]):
    with closing(sqlite3.connect(DB_FILE)) as conn:
        with conn:
            conn.execute("""
                INSERT OR REPLACE INTO triaged_tickets 
                (email_id, payload_type, subject, cc_list, actionable, assigned_owner, target_queue, responsibility, evaluation)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ticket_data["email_id"], ticket_data["payload_type"], ticket_data["subject"],
                ticket_data["cc_list"], ticket_data["actionable"], ticket_data["assigned_owner"],
                ticket_data["target_queue"], ticket_data["responsibility"], ticket_data["evaluation"]
            ))

def fetch_unread_emails() -> List[Dict[str, Any]]:
    logger.info(f"Connecting to IMAP server: {IMAP_SERVER}")
    unprocessed_emails = []
    try:
        with closing(imaplib.IMAP4_SSL(IMAP_SERVER)) as mail:
            mail.login(BOT_EMAIL, BOT_APP_PASSWORD)
            mail.select("inbox")
            status, messages = mail.search(None, "UNSEEN")
            email_ids = messages[0].split()

            if not email_ids:
                return []

            for idx, e_id in enumerate(email_ids, 1):
                email_id_str = e_id.decode("utf-8")
                if is_already_processed(email_id_str): continue

                _, msg_data = mail.fetch(e_id, "(RFC822)")
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        subject, encoding = decode_header(msg.get("Subject", ""))[0]
                        if isinstance(subject, bytes): subject = subject.decode(encoding if encoding else "utf-8")

                        cc_header = msg.get("Cc", "")
                        cc_list = ""
                        if cc_header:
                            cc, encoding = decode_header(cc_header)[0]
                            cc_list = cc.decode(encoding if encoding else "utf-8") if isinstance(cc, bytes) else str(cc)

                        body, images_b64 = "", []
                        if msg.is_multipart():
                            for part in msg.walk():
                                if part.get_content_type() == "text/plain":
                                    body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                                elif part.get_content_maintype() == "image":
                                    img_data = part.get_payload(decode=True)
                                    if img_data: images_b64.append(base64.b64encode(img_data).decode("utf-8"))
                        else:
                            body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

                        unprocessed_emails.append({
                            "email_id": email_id_str, "subject": subject if subject else "(No Subject)",
                            "cc": cc_list, "body": body[:1500], "images": images_b64
                        })
    except Exception as e:
        logger.error(f"IMAP Fetch Failure: {e}")
    return unprocessed_emails

def process_images_with_llava(base64_images: List[str]) -> str:
    if not base64_images: return "No visual evidence attached."
    prompt = "Extract legible text/error codes and describe what the photo depicts physically."
    payload = {"model": OLLAMA_VISION_MODEL, "prompt": prompt, "stream": False, "images": base64_images}
    try:
        return requests.post(OLLAMA_API_URL, json=payload, timeout=180).json()["response"].strip()
    except Exception as e:
        return "Failed to analyze image assets."

def extract_json_from_deepseek(raw_response: str) -> dict:
    cleaned_text = re.sub(r'<think>.*?</think>', '', raw_response, flags=re.DOTALL).strip()
    match = re.search(r'\{.*}', cleaned_text, re.DOTALL)
    if match:
        try: return json.loads(match.group(0))
        except json.JSONDecodeError: pass
    return {}

def stage1_gatekeeper(subject: str, body: str) -> dict:
    logger.info("STAGE 1: Gatekeeper Analysis (DeepSeek-R1)...")
    prompt = STAGE1_GATEKEEPER_PROMPT.format(subject=subject, body=body[:1000])
    payload = {"model": OLLAMA_STAGE1_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.0}}
    try:
        res = requests.post(OLLAMA_API_URL, json=payload).json()["response"]
        return extract_json_from_deepseek(res)
    except Exception as e:
        return {"is_valid_request": False, "reason": "Engine Failure"}

def stage2_skill_router(subject: str, body: str, skill_dict: str) -> str:
    logger.info("STAGE 2: Skill Classification (llama3.1)...")
    prompt = STAGE2_SKILL_ROUTER_PROMPT.format(skill_dictionary=skill_dict, subject=subject, body=body[:1000])
    payload = {"model": OLLAMA_STAGE2_MODEL, "prompt": prompt, "format": "json", "stream": False, "options": {"temperature": 0.0}}
    try:
        res = requests.post(OLLAMA_API_URL, json=payload).json()["response"]
        return json.loads(res).get("responsibility", "Unassigned")
    except Exception as e:
        return "Unassigned"

def run_dispatch_cycle():
    logger.info("Starting dispatch cycle...")
    skills_markdown, skill_to_queue, skill_to_owners = load_relational_maps()

    if not skill_to_queue:
        logger.error("Failed to load Excel data. Aborting.")
        return

    current_workloads = load_or_init_workloads()
    unprocessed = fetch_unread_emails()

    for idx, em in enumerate(unprocessed, 1):
        logger.info(f"Processing ticket [{idx}/{len(unprocessed)}]: '{em['subject'][:30]}'")

        vision_insights = process_images_with_llava(em["images"])

        # STAGE 1: Check if valid
        gatekeeper = stage1_gatekeeper(em["subject"], em["body"])
        is_valid = gatekeeper.get("is_valid_request", False)
        gatekeeper_reason = gatekeeper.get("reason", "N/A")

        responsibility = "Unassigned"
        queue = "Unassigned"
        candidate_pool = []

        if is_valid:
            # STAGE 2: Mistral finds the specific skill
            # (Note: we inject the vision string into the body dynamically for context if images exist)
            full_context = em["body"] + f"\n\n[IMAGE DATA]: {vision_insights}" if em["images"] else em["body"]
            responsibility = stage2_skill_router(em["subject"], full_context, skills_markdown)

            # PURE PYTHON: STAGE 3 RELATIONAL LOOKUP (Instant, no LLM required)
            queue = skill_to_queue.get(responsibility, "Unassigned")
            candidate_pool = skill_to_owners.get(responsibility, [])

        # TOURNAMENT LOGIC
        assigned_owner = "Unassigned"
        tournament_logs = "N/A"

        if is_valid:
            valid_candidates = [str(c).strip() for c in candidate_pool if str(c).strip() not in ["", "Unassigned", "N/A"]]
            if valid_candidates:
                score_cards = [f"{name} ({current_workloads.get(name, 0)} active)" for name in valid_candidates]
                tournament_logs = f"Eligible Candidates Identified: {', '.join(score_cards)}. "

                min_score = min(current_workloads.get(name, 0) for name in valid_candidates)
                tied_candidates = [name for name in valid_candidates if current_workloads.get(name, 0) == min_score]
                assigned_owner = random.choice(tied_candidates)
                tournament_logs += f"🏆 Winner chosen by Python Engine: {assigned_owner} (Score: {min_score})."
            else:
                tournament_logs = "No valid candidates found in Staff Roster for this skill."
        else:
            tournament_logs = f"Ticket skipped by Gatekeeper. Reason: {gatekeeper_reason}"

        ai_thinking = (
            f"🛑 **Gatekeeper:** {'Approved' if is_valid else 'Rejected'} ({gatekeeper_reason})\n\n"
            f"🎯 **Skill Selected by AI:** {responsibility}\n\n"
            f"⚖️ **Python Workload Tournament Result:**\n`{tournament_logs}`"
        )

        ticket_packet = {
            "email_id": em["email_id"], "payload_type": "🖼️ Image+Text" if em["images"] else "📝 Pure Text",
            "subject": em["subject"], "cc_list": em["cc"] if em["cc"] else "-",
            "actionable": "✅ Yes" if is_valid else "❌ No",
            "assigned_owner": assigned_owner, "target_queue": queue,
            "responsibility": responsibility, "evaluation": ai_thinking
        }

        save_triaged_ticket(ticket_packet)

        if is_valid and assigned_owner != "Unassigned":
            update_workload(assigned_owner)
            current_workloads[assigned_owner] = current_workloads.get(assigned_owner, 0) + 1

        logger.info(f"Saved: Owner -> {assigned_owner} | Queue -> {queue}")

if __name__ == "__main__":
    try:
        print("🚀 [BOOT] Initializing V3 Relational Dispatch Engine (2-Model Stack)...")
        init_db()
        logger.info("Engine running successfully. Press Ctrl+C to shut down.")
        while True:
            run_dispatch_cycle()
            time.sleep(60)
    except KeyboardInterrupt:
        print("\n🛑 Shutting down.")
    except Exception as e:
        print(f"💥 ERROR: {e}")