import base64
import email
import imaplib
import json
import os
from email.header import decode_header

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
BOT_EMAIL = os.getenv("BOT_EMAIL")
BOT_APP_PASSWORD = os.getenv("BOT_APP_PASSWORD")
IMAP_SERVER = os.getenv("IMAP_SERVER")

# Model Configuration Pipeline
OLLAMA_VISION_MODEL = "llava-phi3:latest" # Stage 1: The Eyes
OLLAMA_BRAIN_MODEL = "llama3:latest"       # Stage 2: The Brain
OLLAMA_API_URL = "http://localhost:11434/api/generate"

def load_staff_data(filepath="Staff_itrt.xlsx"):
    """Loads the CSV and converts it into a clean Markdown table."""
    try:
        df = pd.read_excel(filepath)
        df = df.fillna("(no value)")
        print(f"[SYSTEM LOG] Loaded responsibility matrix successfully.")
        return df.to_markdown(index=False)
    except Exception as e:
        st.error(f"Error loading Excel file '{filepath}': {e}")
        return ""

def fetch_unread_emails(limit=5):
    """Connects to Gmail IMAP server and retrieves unread emails with CC and Images."""
    print("\n==================================================")
    print(f"[MAIL LOG] Establishing connection to IMAP server: {IMAP_SERVER}")
    emails = []
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(BOT_EMAIL, BOT_APP_PASSWORD)
        mail.select("inbox")

        status, messages = mail.search(None, "UNSEEN")
        email_ids = messages[0].split()

        if not email_ids:
            print("[MAIL LOG] Inbox folder checked. 0 new unread emails found.")
            mail.logout()
            return []

        selected_ids = email_ids[-limit:]
        print(f"[MAIL LOG] Found {len(email_ids)} unread emails. Pulling the latest {len(selected_ids)} items...")

        for idx, e_id in enumerate(selected_ids, 1):
            _, msg_data = mail.fetch(e_id, "(RFC822)")
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])

                    # Parse Subject Line
                    subject, encoding = decode_header(msg.get("Subject", ""))[0]
                    if isinstance(subject, bytes):
                        subject = subject.decode(encoding if encoding else "utf-8")

                    # Parse CC Header
                    cc_header = msg.get("Cc", "")
                    cc_list = ""
                    if cc_header:
                        cc, encoding = decode_header(cc_header)[0]
                        if isinstance(cc, bytes):
                            cc_list = cc.decode(encoding if encoding else "utf-8")
                        else:
                            cc_list = cc

                    # Parse Multi-part body and attachments
                    body = ""
                    images_b64 = []

                    if msg.is_multipart():
                        for part in msg.walk():
                            content_type = part.get_content_type()

                            if content_type == "text/plain":
                                try:
                                    body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                                except:
                                    pass
                            elif part.get_content_maintype() == "image":
                                img_data = part.get_payload(decode=True)
                                if img_data:
                                    b64_img = base64.b64encode(img_data).decode("utf-8")
                                    images_b64.append(b64_img)
                                    print(f"      [!] Extracted attached screenshot.")
                    else:
                        body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

                    print(f"  -> [{idx}/{len(selected_ids)}] Downloaded Subject: '{subject[:40]}...'")
                    emails.append({
                        "id": e_id,
                        "subject": subject,
                        "cc": cc_list,
                        "body": body[:1500],
                        "images": images_b64
                    })
        mail.logout()
        print("[MAIL LOG] IMAP connection safely closed.")
    except Exception as e:
        st.error(f"IMAP Fetching Crash: {e}")
    return emails

def process_images_with_llava(base64_images):
    """STAGE 1 (The Eyes): Ask Llava-Phi3 to extract both literal text and physical context."""
    if not base64_images:
        return "No visual evidence attached."

    print(f"[STAGE 1 - VISION] Passing {len(base64_images)} camera/screenshot uploads to Llava-Phi3...")

    prompt = (
        "Analyze this IT support image. It could be a clean desktop screenshot OR a phone camera photo "
        "of a physical screen, device, or hardware environment. Provide your analysis in exactly two sections:\n\n"
        "1. TEXT EXTRACTED: Extract all legible text, error codes, system messages, or button labels word-for-word. "
        "If no text is visible, write 'None'.\n"
        "2. PHOTO CONTEXT & DESCRIPTION: Describe what the photo physically depicts. Is it a photo of a desktop monitor, "
        "a physical printer chassis, a server rack, a broken cable, or an office environment? Explain what is happening "
        "in the scene."
    )

    payload = {
        "model": OLLAMA_VISION_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.0}
    }
    payload["images"] = base64_images

    try:
        response = requests.post(OLLAMA_API_URL, json=payload, timeout=180)
        response.raise_for_status()
        description = response.json()["response"].strip()
        print(f"[STAGE 1 - SUCCESS] Dual-context image extraction complete.")
        return description
    except Exception as e:
        print(f"[STAGE 1 - ERROR] Vision failure: {str(e)}")
        return f"Failed to analyze image assets: {str(e)}"
def analyze_email_with_llama3(subject, cc_list, body, vision_description, markdown_matrix):
    """STAGE 2 (The Brain): Routes tickets by explicitly matching data inputs against the operational matrix."""
    print(f"[STAGE 2 - BRAIN] Processing routing logic for: '{subject[:40]}...'")

    prompt = f"""
    You are an elite IT Ticket Routing Engine. Your goal is to map an incoming support request to the single most appropriate staff member listed in the responsibility matrix.
    You MUST return your output strictly as a single, well-formed JSON object matching the required schema properties provided below.

    ========================================================================
    IT RESPONSIBILITY MATRIX (THE AVAILABLE POOL):
    ========================================================================
    {markdown_matrix}

    ========================================================================
    INCOMING TICKET RAW DATA:
    ========================================================================
    CRITICAL: You must analyze all three dimensions below, paying extra attention to the intent established in the Subject Line.
    
    - CRITICAL SUBJECT LINE: {subject}
    - CC RECIPIENTS: {cc_list if cc_list else 'None'}
    - MESSAGE BODY text: {body}
    
    - VISUAL EVIDENCE ANALYSIS (From Photo/Screenshot): 
    {vision_description}

    CRITICAL DISPATCH RULES:
    1. SUBJECT INTEGRITY: Never ignore the subject line. It often contains the primary summary of the request, location, or system name.
    2. SCOPE BOUNDARY CLASSIFICATION: Before shortlisting, determine the operational environment of the request from the subject, body, or image context:
       - Is this an Institutional/Academic/Core Network issue?
       - Is this a Residential/Housing/Accommodation area issue?
       Match the ticket to the staff member whose listed domain explicitly encompasses that specific operational environment scope.
    3. FUNCTIONAL VERB MATCHING: Match the underlying business action (e.g., financial transactions, administrative workflows, student services, or physical infrastructure repairs) to the candidate whose description closely mirrors those core responsibilities.
    4. BEST-FIT DETERMINATION: If no candidate is a perfect text-string match but the request is a valid IT issue, do not return null. Evaluate the shortlist and assign it to the staff member whose listed duties make them the most logical and closest operational fit.

    Populate every single string field in the JSON schema below by working through the routing problem step-by-step:

    JSON Schema Format Required:
    {{
      "request_diagnostics": "In 1 sentence, summarize the issue.",
      "targeted_matrix_queue": "Identify ONE specific Department Queue from the matrix.",
      "exact_responsibility_match": "CRITICAL: Look ONLY inside the targeted queue. Copy the exact string from the 'Responsibility' column that matches the issue (e.g., 'Internet service in residential areas'). NEVER pick 'General' if a specific role fits.",
      "matched_row_description": "Copy the exact description for the responsibility you selected above.",
      "candidate_shortlist": "List ONLY the owners who hold this exact Responsibility in this exact Queue.",
      "elimination_round": "FIREWALL RULE: You are strictly forbidden from considering a candidate's roles in other queues. Evaluate them ONLY on what they do inside this specific queue.",
      "final_winner_justification": "State the final surviving candidate.",
      "is_valid_request": true,
      "queue": "Exact Queue string",
      "owner": "Exact Owner string",
      "responsibility": "Exact Responsibility string",
      "request_type": "..."
    }}
    """

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

    try:
        response = requests.post(OLLAMA_API_URL, json=payload, timeout=180)
        response.raise_for_status()
        raw_text = response.json()["response"]

        data = json.loads(raw_text.strip())

        ai_thinking = (
            f"- 1. Diagnostics: {data.get('request_diagnostics', 'N/A')}\n"
            f"- 2. Shortlist: {data.get('candidate_shortlist', 'N/A')}\n"
            f"- 3. Elimination: {data.get('elimination_round', 'N/A')}\n"
            f"- 4. Winner: {data.get('final_winner_justification', 'N/A')}"
        )
        data["candidate_evaluation"] = ai_thinking
        return data

    except Exception as e:
        print(f"[STAGE 2 - ERROR] Structural logic failure: {str(e)}")
        return {
            "is_valid_request": False,
            "queue": None,
            "owner": None,
            "responsibility": None,
            "request_type": "(no value)",
            "candidate_evaluation": f"Llama3 generation crash: {str(e)}"
        }

# --- Streamlit UI Layout Configuration ---
st.set_page_config(page_title="Real-Time IT AI Dispatcher", layout="wide")
st.title("🤖 Orchestrated IT Dispatcher (Llava-Phi3 Eyes + Llama3 Brain)")
st.markdown("Chained Multi-Model Pipeline: Vision Extraction ➡️ Contextual Verb Triage.")

if "analyzed_data" not in st.session_state:
    st.session_state.analyzed_data = []

col1, col2 = st.columns([1, 4])
with col1:
    fetch_btn = st.button("Fetch & Analyze New Emails", type="primary", use_container_width=True)
with col2:
    if st.button("Clear Dashboard", use_container_width=True):
        st.session_state.analyzed_data = []
        st.rerun()

table_placeholder = st.empty()

if st.session_state.analyzed_data:
    table_placeholder.dataframe(pd.DataFrame(st.session_state.analyzed_data), use_container_width=True)

if fetch_btn:
    with st.spinner("Executing model routing matrix..."):
        markdown_matrix = load_staff_data("Staff_itrt.xlsx")

        if markdown_matrix:
            new_emails = fetch_unread_emails(limit=5)

            if not new_emails:
                st.info("No new unread emails found.")
            else:
                for idx, em in enumerate(new_emails, 1):
                    has_img_flag = "🖼️ Image+Text" if em["images"] else "📝 Pure Text"
                    st.toast(f"Processing ticket {idx}/{len(new_emails)} through pipeline...")

                    # Step 1: Visual translation via Llava-Phi3
                    vision_insights = ""
                    if em["images"]:
                        with st.spinner(f"Extracting UI details from screenshot ({idx}/{len(new_emails)})..."):
                            vision_insights = process_images_with_llava(em["images"])
                    else:
                        vision_insights = "No images attached to this request."

                    # Step 2: Logical routing via Llama3
                    analysis = analyze_email_with_llama3(
                        em["subject"], em["cc"], em["body"], vision_insights, markdown_matrix
                    )

                    row = {
                        "Payload Type": has_img_flag,
                        "Subject Line": em["subject"],
                        "CC Field Data": em["cc"] if em["cc"] else "-",
                        "Actionable?": "✅ Yes" if analysis.get("is_valid_request") else "❌ No",
                        "Assigned Owner": analysis.get("owner") if analysis.get("owner") else "Unassigned",
                        "Target Queue": analysis.get("queue") if analysis.get("queue") else "Unassigned",
                        "Responsibility": analysis.get("responsibility") if analysis.get("responsibility") else "Unassigned",
                        "Tournament Evaluation Reasoning": analysis.get("candidate_evaluation") if analysis.get("candidate_evaluation") else "-"
                    }

                    st.session_state.analyzed_data.append(row)
                    df_results = pd.DataFrame(st.session_state.analyzed_data)

                    table_placeholder.dataframe(
                        df_results,
                        width='stretch',
                        column_config={
                            "Tournament Evaluation Reasoning": st.column_config.TextColumn("Tournament Evaluation Reasoning", width="large")
                        }
                    )

                st.success("Triage processing complete across both local model instances!")