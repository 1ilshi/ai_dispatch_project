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
import difflib

from contextlib import closing
from email.header import decode_header
from typing import List, Dict, Any
from email.utils import parsedate_to_datetime

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

# NEW: Enterprise deployment variables
# Example .env entry: SENIOR_STAFF="Viraphan,Harianto"
SENIOR_STAFF_ENV = os.getenv("SENIOR_STAFF", "Viraphan,Harianto")
SENIOR_STAFF = [name.strip() for name in SENIOR_STAFF_ENV.split(",") if name.strip()]

# THE ULTRA-FAST 2-MODEL STACK
OLLAMA_VISION_MODEL = "llava-phi3:latest"
OLLAMA_STAGE1_MODEL = "deepseek-r1:1.5b"  # Gatekeeper
OLLAMA_STAGE2_MODEL = "llama3.1:latest"    # Skill Router
OLLAMA_API_URL = "http://localhost:11434/api/generate"

from prompts import STAGE1_GATEKEEPER_PROMPT, STAGE2_SKILL_ROUTER_PROMPT

DB_FILE = "tickets.db"
WORKLOAD_FILE = "workloads.json"
MATRIX_FILE = "IT_Routing_Matrix_sorted.xlsx"

# ==========================================
# 1. DATA AND INFRASTRUCTURE LAYER
# ==========================================

def init_db():
    with closing(sqlite3.connect(DB_FILE)) as conn:
        with conn:
            conn.execute("""
                         CREATE TABLE IF NOT EXISTS triaged_tickets (
                                                                        email_id TEXT PRIMARY KEY,
                                                                        email_date DATETIME,
                                                                        payload_type TEXT,
                                                                        ticket_type TEXT,
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
    try:
        skills_df = pd.read_excel(filepath, sheet_name="Skill_Dictionary")
        queue_df = pd.read_excel(filepath, sheet_name="Queue_Map")
        roster_df = pd.read_excel(filepath, sheet_name="Staff_Roster")

        try:
            types_df = pd.read_excel(filepath, sheet_name="Ticket_Types", header=None)
            if str(types_df.iloc[0, 0]).strip().lower() in ["ticket_type", "ticket type", "type"]:
                types_df = types_df.iloc[1:].reset_index(drop=True)
            types_df.columns = ["Ticket_Type", "Description"]
            ticket_types_markdown = types_df.to_markdown(index=False)
            allowed_types_list = "\n".join([f'   - "{str(t).strip()}"' for t in types_df["Ticket_Type"].dropna()])
        except Exception as e:
            ticket_types_markdown = "Fallback ticket types..."
            allowed_types_list = '   - "Problem Solving"'

        try:
            acronyms_df = pd.read_excel(filepath, sheet_name="Acronyms", header=None)
            if str(acronyms_df.iloc[0, 0]).strip().lower() in ["acronym", "abbr", "abbreviation"]:
                acronyms_df = acronyms_df.iloc[1:].reset_index(drop=True)
            acronyms_df = acronyms_df.dropna(subset=[acronyms_df.columns[0]])
            acronyms_list = [f"- {str(row[acronyms_df.columns[0]]).strip()}: {str(row[acronyms_df.columns[1]]).strip()}" for _, row in acronyms_df.iterrows()]
            acronyms_markdown = "\n".join(acronyms_list) if acronyms_list else "No acronyms loaded."
        except Exception as e:
            acronyms_markdown = "No custom acronyms defined yet."

        skills_markdown = skills_df.to_markdown(index=False)
        skill_to_queue = dict(zip(queue_df['Unique_Skill'], queue_df['Queue']))
        skill_to_owners = roster_df.groupby('Unique_Skill')['Owner'].apply(lambda x: list(x.astype(str))).to_dict()

        return skills_markdown, skill_to_queue, skill_to_owners, ticket_types_markdown, allowed_types_list, acronyms_markdown
    except Exception as e:
        logger.error(f"Error loading relational maps: {e}")
        return None, {}, {}, "", "", ""

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
    if not owner_name or owner_name == "Unassigned": return
    if os.path.exists(WORKLOAD_FILE):
        with open(WORKLOAD_FILE, "r") as f: workloads = json.load(f)
        workloads[owner_name] = workloads.get(owner_name, 0) + 1
        with open(WORKLOAD_FILE, "w") as f: json.dump(workloads, f)

# ==========================================
# 2. EMAIL INTEGRATION LAYER (Removable for RT)
# ==========================================

def fetch_unread_emails() -> List[Dict[str, Any]]:
    logger.info(f"Connecting to IMAP server: {IMAP_SERVER}")
    unprocessed_emails = []
    try:
        with closing(imaplib.IMAP4_SSL(IMAP_SERVER)) as mail:
            mail.login(BOT_EMAIL, BOT_APP_PASSWORD)
            mail.select("inbox")
            status, messages = mail.search(None, "UNSEEN")
            email_ids = messages[0].split()

            if not email_ids: return []

            for e_id in email_ids:
                email_id_str = e_id.decode("utf-8")

                # Check DB directly
                with closing(sqlite3.connect(DB_FILE)) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT 1 FROM triaged_tickets WHERE email_id = ?", (email_id_str,))
                    if cursor.fetchone(): continue

                _, msg_data = mail.fetch(e_id, "(RFC822)")
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        subject, encoding = decode_header(msg.get("Subject", ""))[0]
                        if isinstance(subject, bytes): subject = subject.decode(encoding if encoding else "utf-8")

                        date_header = msg.get("Date")
                        email_date = parsedate_to_datetime(date_header).strftime("%Y-%m-%d %H:%M:%S") if date_header else time.strftime("%Y-%m-%d %H:%M:%S")

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
                            "email_id": email_id_str, "email_date": email_date,
                            "subject": subject if subject else "(No Subject)",
                            "cc": cc_list, "body": body[:1500], "images": images_b64
                        })
    except Exception as e:
        logger.error(f"IMAP Fetch Failure: {e}")
    return unprocessed_emails

# ==========================================
# 3. AI PROCESSING BRAIN (Fully Decoupled - RT Ready)
# ==========================================

def process_images_with_llava(base64_images: List[str]) -> str:
    if not base64_images: return "No visual evidence attached."
    prompt = "Extract legible text/error codes and describe what the photo depicts physically."
    payload = {"model": OLLAMA_VISION_MODEL, "prompt": prompt, "stream": False, "images": base64_images}
    try: return requests.post(OLLAMA_API_URL, json=payload, timeout=180).json()["response"].strip()
    except Exception: return "Failed to analyze image assets."

def extract_json_from_deepseek(raw_response: str) -> dict:
    cleaned_text = re.sub(r'<think>.*?</think>', '', raw_response, flags=re.DOTALL).strip()
    match = re.search(r'\{.*?}', cleaned_text, re.DOTALL)
    if match:
        try: return json.loads(match.group(0))
        except json.JSONDecodeError: pass
    return {"is_valid_request": False, "reason": "Gatekeeper formatting failure."}

def stage1_gatekeeper(subject: str, body: str) -> dict:
    prompt = STAGE1_GATEKEEPER_PROMPT.replace("{subject}", str(subject)).replace("{body}", str(body[:1000]))
    payload = {"model": OLLAMA_STAGE1_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.0}}
    try: return extract_json_from_deepseek(requests.post(OLLAMA_API_URL, json=payload).json()["response"])
    except Exception: return {"is_valid_request": False, "reason": "Engine Failure"}


def stage2_skill_router(subject: str, body: str, skill_dict: str, types_markdown: str, allowed_types_list: str, acronyms_markdown: str, valid_skills_list: list) -> tuple:
    """Classifies ticket type and assigns the handling responsibility."""
    logger.info("STAGE 2: Skill Classification (llama3.1)...")

    prompt = (STAGE2_SKILL_ROUTER_PROMPT
              .replace("{skill_dictionary}", str(skill_dict))
              .replace("{ticket_types_dictionary}", str(types_markdown))
              .replace("{allowed_ticket_types}", str(allowed_types_list))
              .replace("{acronyms_dictionary}", str(acronyms_markdown))
              .replace("{subject}", str(subject))
              .replace("{body}", str(body[:1000])))

    payload = {
        "model": OLLAMA_STAGE2_MODEL,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        "options": {
            "temperature": 0.0,
            "num_ctx": 16384
        }
    }

    try:
        res = requests.post(OLLAMA_API_URL, json=payload).json()["response"]

        match = re.search(r'\{.*?}', res, re.DOTALL)
        raw_json = match.group(0) if match else res.strip()
        parsed_json = json.loads(raw_json)

        clean_json = {str(k).strip(): str(v).strip() for k, v in parsed_json.items()}

        raw_responsibility = clean_json.get("responsibility", "Unassigned")
        ticket_type = clean_json.get("ticket_type", "Problem Solving")
        resp_reason = clean_json.get("responsibility_reason", "No reason provided.")

        # ==========================================
        # 🛡️ THE HALLUCINATION AUTO-CORRECTOR 🛡️
        # ==========================================
        final_responsibility = "Unassigned"
        if raw_responsibility in valid_skills_list:
            final_responsibility = raw_responsibility
        else:
            # If the AI hallucinates, find the closest actual match in the Excel sheet!
            closest_matches = difflib.get_close_matches(raw_responsibility, valid_skills_list, n=1, cutoff=0.3)
            if closest_matches:
                final_responsibility = closest_matches[0]
                logger.warning(f"AI Hallucinated '{raw_responsibility}'. Auto-corrected to '{final_responsibility}'")
            else:
                final_responsibility = "General_Application" # Ultimate fallback
                logger.warning(f"AI Hallucinated heavily '{raw_responsibility}'. Fell back to General_Application.")

        return final_responsibility, ticket_type, resp_reason
    except Exception as e:
        logger.error(f"Stage 2 Routing Error: {e}")
        return "Unassigned", "Problem Solving", "Error generating reason."

def evaluate_ticket(subject: str, body: str, images_b64: list, current_workloads: dict,
                    skills_markdown: str, skill_to_queue: dict, skill_to_owners: dict,
                    types_markdown: str, allowed_types_list: str, acronyms_markdown: str) -> dict:

    # 1. PROCESS IMAGES FIRST! (So Gatekeeper can see them)
    vision_insights = process_images_with_llava(images_b64) if images_b64 else ""
    full_context = body + f"\n\n[IMAGE DATA]: {vision_insights}" if images_b64 else body

    # 2. RUN GATEKEEPER WITH THE FULL CONTEXT
    gatekeeper = stage1_gatekeeper(subject, full_context)
    is_valid = gatekeeper.get("is_valid_request", False)
    gatekeeper_reason = gatekeeper.get("reason", "N/A")

    if not is_valid:
        return {
            "actionable": False, "gatekeeper_reason": gatekeeper_reason,
            "responsibility": "N/A", "ticket_type": "N/A", "queue": "N/A",
            "assigned_owner": "Unassigned", "tournament_logs": f"Rejected by Gatekeeper: {gatekeeper_reason}",
            "ai_thinking": f"🛑 **Gatekeeper:** Rejected ({gatekeeper_reason})"
        }

    # 3. EXTRACT VALID SKILLS LIST FOR THE AUTO-CORRECTOR
    valid_skills_list = list(skill_to_queue.keys())

    # 4. RUN SKILL ROUTER (Only once!)
    responsibility, ticket_type, resp_reason = stage2_skill_router(
        subject, full_context, skills_markdown, types_markdown, allowed_types_list, acronyms_markdown, valid_skills_list
    )

    queue = skill_to_queue.get(responsibility, "Unassigned")
    candidate_pool = skill_to_owners.get(responsibility, [])

    valid_candidates = [str(c).strip() for c in candidate_pool if str(c).strip() not in ["", "Unassigned", "N/A"]]
    junior_candidates = [c for c in valid_candidates if c not in SENIOR_STAFF]

    if junior_candidates: valid_candidates = junior_candidates

    assigned_owner = "Unassigned"
    tournament_logs = "No valid candidates found."
    if valid_candidates:
        min_score = min(current_workloads.get(name, 0) for name in valid_candidates)
        tied_candidates = [name for name in valid_candidates if current_workloads.get(name, 0) == min_score]
        assigned_owner = random.choice(tied_candidates)
        score_cards = [f"{name} ({current_workloads.get(name, 0)})" for name in valid_candidates]
        tournament_logs = f"Candidates: {', '.join(score_cards)}. 🏆 Winner: {assigned_owner} (Score: {min_score})."

    ai_thinking = (
        f"🛑 **Gatekeeper:** Approved ({gatekeeper_reason})\n\n"
        f"🏷️ **Ticket Type:** {ticket_type}\n\n"
        f"🎯 **Skill Selected:** {responsibility}\n💡 **Reason:** {resp_reason}\n\n"
        f"⚖️ **Workload Result:**\n`{tournament_logs}`"
    )

    return {
        "actionable": True, "gatekeeper_reason": gatekeeper_reason,
        "responsibility": responsibility, "ticket_type": ticket_type, "queue": queue,
        "assigned_owner": assigned_owner, "tournament_logs": tournament_logs,
        "ai_thinking": ai_thinking
    }

# ==========================================
# 4. EXECUTION LOOP
# ==========================================

def run_dispatch_cycle():
    logger.info("Starting dispatch cycle...")
    maps = load_relational_maps()
    if not maps[1]:
        logger.error("Failed to load Excel data. Aborting.")
        return

    skills_md, skill_to_queue, skill_to_owners, types_md, allowed_types, acronyms_md = maps
    current_workloads = load_or_init_workloads()
    unprocessed = fetch_unread_emails()

    for idx, em in enumerate(unprocessed, 1):
        logger.info(f"Processing ticket [{idx}/{len(unprocessed)}]: '{em['subject'][:30]}'")

        # Call the decoupled AI Brain
        decision = evaluate_ticket(
            em["subject"], em["body"], em["images"], current_workloads,
            skills_md, skill_to_queue, skill_to_owners, types_md, allowed_types, acronyms_md
        )

        ticket_packet = {
            "email_id": em["email_id"], "email_date": em["email_date"],
            "payload_type": "🖼️ Image+Text" if em["images"] else "📝 Pure Text",
            "ticket_type": decision["ticket_type"],
            "subject": em["subject"], "cc_list": em["cc"] if em["cc"] else "-",
            "actionable": "✅ Yes" if decision["actionable"] else "❌ No",
            "assigned_owner": decision["assigned_owner"],
            "target_queue": decision["queue"],
            "responsibility": decision["responsibility"],
            "evaluation": decision["ai_thinking"]
        }

        with closing(sqlite3.connect(DB_FILE)) as conn:
            with conn:
                conn.execute("""
                    INSERT OR REPLACE INTO triaged_tickets 
                    (email_id, email_date, payload_type, ticket_type, subject, cc_list, actionable, assigned_owner, target_queue, responsibility, evaluation)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, tuple(ticket_packet.values()))

        if decision["actionable"] and decision["assigned_owner"] != "Unassigned":
            update_workload(decision["assigned_owner"])
            current_workloads[decision["assigned_owner"]] = current_workloads.get(decision["assigned_owner"], 0) + 1

        logger.info(f"Saved: Owner -> {decision['assigned_owner']} | Queue -> {decision['queue']}")

if __name__ == "__main__":
    try:
        print("🚀 [BOOT] Initializing V3 Relational Dispatch Engine (RT-Ready Modular Build)...")
        init_db()
        logger.info("Engine running successfully. Press Ctrl+C to shut down.")
        while True:
            run_dispatch_cycle()
            time.sleep(60)
    except KeyboardInterrupt:
        print("\n🛑 Shutting down.")
    except Exception as e:
        print(f"💥 ERROR: {e}")