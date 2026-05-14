"""
Weekly sync: re-embeds all company and client records into the embeddings table.
Run manually: python sync_embeddings.py
Scheduled:    crontab -e  →  0 2 * * 1 cd ~/titan-rag-bot && python sync_embeddings.py
"""

import json
import logging
import os
import time
from dotenv import load_dotenv

load_dotenv()

from openai import OpenAI
from supabase import create_client

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BATCH_SIZE = 50  # OpenAI embeddings per request


def build_company_text(row: dict) -> str:
    parts = [
        f"Type: company",
        f"Company: {row.get('company_name', '')}",
        f"Brand: {row.get('brand', '')}",
        f"Industry: {row.get('industry', '')}",
        f"Nature of Business: {row.get('nature_of_business', '')}",
        f"Location: {row.get('location', '')}",
        f"Total Duration: {row.get('total_duration', '')}",
        f"Made in Spire: {row.get('made_in_spire', '')}",
        f"Register No: {row.get('register_no', '')}",
        f"Entiti Bisnes: {row.get('entiti_bisnes', '')}",
        f"Partner in Circle: {row.get('partner_in_circle', '')}",
    ]
    return " | ".join(p for p in parts if not p.endswith(": "))


def build_client_text(row: dict, company_name: str = "") -> str:
    parts = [
        f"Type: client",
        f"Name: {row.get('name', '')}",
        f"Company: {company_name}",
        f"CAM: {row.get('cam', '')}",
        f"Email: {row.get('email', '')}",
        f"Phone: {row.get('phone', '')}",
        f"IC No: {row.get('ic_no', '')}",
        f"Start Date: {row.get('start_date', '')}",
        f"Expiry Date: {row.get('expiry_date', '')}",
        f"Pax: {row.get('pax', '')}",
    ]
    return " | ".join(p for p in parts if not p.endswith(": "))


def embed_batch(client: OpenAI, texts: list[str]) -> list[list[float]]:
    resp = client.embeddings.create(model="text-embedding-3-small", input=texts)
    return [item.embedding for item in resp.data]


def sync():
    supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    openai = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    # --- Build documents ---
    documents = []  # {"content": str, "metadata": dict}

    logger.info("Fetching companies...")
    companies = supabase.from_("company_details").select("*").execute().data or []
    company_map = {r["company_id"]: r.get("company_name", "") for r in companies}
    for row in companies:
        documents.append({
            "content": build_company_text(row),
            "metadata": {"type": "company", "company_id": row["company_id"]},
        })
    logger.info("  %d companies loaded", len(companies))

    logger.info("Fetching clients...")
    clients = supabase.from_("client_details").select("*").execute().data or []
    for row in clients:
        company_name = company_map.get(row.get("company_id", ""), "")
        documents.append({
            "content": build_client_text(row, company_name),
            "metadata": {"type": "client", "member_id": row["member_id"]},
        })
    logger.info("  %d clients loaded", len(clients))

    # --- Embed in batches ---
    logger.info("Generating embeddings for %d documents...", len(documents))
    embeddings = []
    for i in range(0, len(documents), BATCH_SIZE):
        batch = documents[i: i + BATCH_SIZE]
        texts = [d["content"] for d in batch]
        vecs = embed_batch(openai, texts)
        embeddings.extend(vecs)
        logger.info("  Embedded %d / %d", min(i + BATCH_SIZE, len(documents)), len(documents))
        time.sleep(0.5)  # stay within rate limits

    # --- Replace embeddings table ---
    logger.info("Clearing old embeddings...")
    supabase.from_("embeddings").delete().neq("id", 0).execute()

    logger.info("Inserting %d new embeddings...", len(documents))
    rows = [
        {
            "content": documents[i]["content"],
            "metadata": documents[i]["metadata"],
            "embedding": embeddings[i],
        }
        for i in range(len(documents))
    ]

    # Insert in chunks (Supabase has a request size limit)
    for i in range(0, len(rows), BATCH_SIZE):
        supabase.from_("embeddings").insert(rows[i: i + BATCH_SIZE]).execute()
        logger.info("  Inserted %d / %d", min(i + BATCH_SIZE, len(rows)), len(rows))

    logger.info("Sync complete. %d embeddings updated.", len(rows))


if __name__ == "__main__":
    sync()
