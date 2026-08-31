"""
Telugu Indic-Alpaca Corpus Ingestion & Irregularity Cleaner Script
Downloads/loads datasets from Hugging Face (e.g. Telugu-LLM-Labs/indic-alpaca-datasets)
Cleans irregularities, normalizes Telugu Unicode, removes broken translations/tags,
and formats data into standardized Llama-3 / ChatML instruction format.
"""

import os
import re
import json
import sys
from typing import List, Dict, Any

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "telugu_indic_alpaca_cleaned.jsonl")

# Standard Hugging Face datasets from Telugu-LLM-Labs collection
TARGET_DATASETS = [
    "Telugu-LLM-Labs/indic-alpaca-telugu",
    "Telugu-LLM-Labs/telugu_alpaca_yadnya",
    "Telugu-LLM-Labs/telugu_dolly",
    "Telugu-LLM-Labs/telugu_instructions_v1",
]

# Irregularity patterns to clean out
REGEX_HTML = re.compile(r"<[^>]+>")
REGEX_MULTIPLE_SPACES = re.compile(r"\s+")
REGEX_ENGLISH_RAW_TAGS = re.compile(r"\[(?:INST|USER|SYSTEM|RESPONSE|INPUT|OUTPUT)\]", re.IGNORECASE)
REGEX_MALFORMED_UNICODE = re.compile(r"[\uFFFD\u0000-\u0008\u000B\u000C\u000E-\u001F]")

TELUGU_SYSTEM_PROMPT = (
    "మీరు అత్యంత నమ్మకమైన, సహాయపడే మరియు సహజమైన తెలుగు AI అసిస్టెంట్. "
    "సమాధానాలు స్పష్టంగా, సహజమైన తెలుగు శైలి మరియు వాడుక భాషలో ఇవ్వండి."
)


def normalize_telugu_text(text: str) -> str:
    """Clean and normalize Telugu text strings, removing corrupted tokens and bad tags."""
    if not text or not isinstance(text, str):
        return ""

    # Remove malformed unicode and raw prompt tokens
    text = REGEX_MALFORMED_UNICODE.sub("", text)
    text = REGEX_ENGLISH_RAW_TAGS.sub("", text)
    text = REGEX_HTML.sub("", text)

    # Replace multiple spaces/newlines
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = REGEX_MULTIPLE_SPACES.sub(" ", text).strip()
    return text


def is_valid_pair(instruction: str, response: str) -> bool:
    """Validate instruction-response pair to remove noise, duplicates, and empty samples."""
    if len(instruction) < 3 or len(response) < 3:
        return False

    # Check if response is just repeating instruction
    if instruction.strip().lower() == response.strip().lower():
        return False

    # Check if text is predominantly gibberish or non-text
    telugu_chars = len(re.findall(r"[\u0C00-\u0C7F]", response))
    english_chars = len(re.findall(r"[a-zA-Z]", response))
    if telugu_chars == 0 and english_chars < 5:
        return False

    return True


def format_chatml_entry(instruction: str, input_context: str, response: str) -> Dict[str, Any]:
    """Format single sample into ChatML / Llama-3 messages format."""
    user_content = instruction
    if input_context and len(input_context.strip()) > 0:
        user_content = f"{instruction}\n\nపరిశీలించవలసిన సందర్భం / వివరాలు:\n{input_context}"

    return {
        "messages": [
            {"role": "system", "content": TELUGU_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": response},
        ]
    }


def generate_fallback_sample_corpus() -> List[Dict[str, Any]]:
    """Generates high-quality authentic Telugu slang & conversational sample dataset if offline."""
    raw_samples = [
        {
            "instruction": "నమస్తే! ఈ రోజు వాతావరణం ఎలా ఉంది?",
            "context": "",
            "response": "నమస్కారం బాస్! వాతావరణం చాలా ప్రశాంతంగా ఉంది. ఈ రోజు పనులు ప్లాన్ చేసుకోవడానికి సూపర్ టైమ్!",
        },
        {
            "instruction": "ఎంటి సంగతులు? ఏం చేస్తున్నావు?",
            "context": "",
            "response": "ఏం లేదు మామా! నీ ప్రశ్నలకి పర్ఫెక్ట్ సమాధానాలు రెడీ చేస్తున్నా. నీ సంగతులేంటి? వర్క్ ఎలా సాగుతోంది?",
        },
        {
            "instruction": "Machine learning అంటే సరళమైన భాషలో వివరించు.",
            "context": "",
            "response": "చాలా సింపుల్ రా బాస్! కంప్యూటర్‌కి మనమే కోడ్ రాయకుండా, గతంలో జరిగిన డేటాను చూపిస్తూ దాన్నుంచి నేర్చుకునేలా చేయడమే Machine Learning. ఉదాహరణకి మన అలవాట్లు చూసి యూట్యూబ్ రికమండేషన్స్ ఇవ్వడం లాంటిది!",
        },
        {
            "instruction": "Python లో లిస్ట్ మరియు ట్యూపుల్ మధ్య తేడా ఏంటి?",
            "context": "",
            "response": "Python లో లిస్ట్ (List) మ్యూటబుల్ — అంటే అందులో ఉన్న ఐటమ్స్‌ని మార్చవచ్చు లేదా కొత్తవి చేర్చవచ్చు `[1, 2, 3]`. కానీ ట్యూపుల్ (Tuple) ఇమ్మ్యూటబుల్ `(1, 2, 3)` — ఒక్కసారి క్రియేట్ చేస్తే మార్చలేము. పర్ఫార్మెన్స్ పరంగా ట్యూపుల్ కొంచెం ఫాస్ట్ ఉంటుంది!",
        },
        {
            "instruction": "ఈ ప్రాజెక్ట్‌లో బగ్స్ వస్తే ఎలా హ్యాండిల్ చేయాలి?",
            "context": "",
            "response": "టెన్షన్ పడాల్సిన పనే లేదు మామా! ఫస్ట్ ఎర్రర్ లాగ్స్ జాగ్రత్తగా రీడ్ చెయ్. ఎక్కడ ఫెయిల్ అవుతుందో స్టెప్ బై స్టెప్ డిబగ్ చేస్తే చిటికెలో సాల్వ్ అవుతుంది. చూద్దాంలే అస్సలు తగ్గదు!",
        },
    ]

    cleaned = []
    for item in raw_samples:
        inst = normalize_telugu_text(item["instruction"])
        ctx = normalize_telugu_text(item["context"])
        resp = normalize_telugu_text(item["response"])
        if is_valid_pair(inst, resp):
            cleaned.append(format_chatml_entry(inst, ctx, resp))
    return cleaned


def run_corpus_cleaning():
    """Main execution method to fetch, clean, and store Telugu dataset."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    cleaned_records = []

    try:
        from datasets import load_dataset
        print("Fetching Hugging Face datasets from Telugu-LLM-Labs collection...")

        for ds_name in TARGET_DATASETS:
            try:
                print(f"Processing dataset: {ds_name}")
                ds = load_dataset(ds_name, split="train")

                for row in ds:
                    inst = normalize_telugu_text(row.get("instruction", "") or row.get("prompt", ""))
                    ctx = normalize_telugu_text(row.get("input", "") or row.get("context", ""))
                    resp = normalize_telugu_text(row.get("output", "") or row.get("response", ""))

                    if is_valid_pair(inst, resp):
                        cleaned_records.append(format_chatml_entry(inst, ctx, resp))
            except Exception as e:
                print(f"Skipping {ds_name} (Network or availability note: {e})")

    except ImportError:
        print("Hugging Face `datasets` package not found. Generating high-quality Telugu corpus template...")

    if not cleaned_records:
        print("Using curated fallback Telugu conversational corpus samples...")
        cleaned_records = generate_fallback_sample_corpus()

    print(f"Writing {len(cleaned_records)} cleaned Telugu records to {OUTPUT_FILE}...")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for entry in cleaned_records:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"Success! Cleaned dataset ready at: {OUTPUT_FILE}")
    return len(cleaned_records)


if __name__ == "__main__":
    count = run_corpus_cleaning()
    print(f"Pipeline completed with {count} verified records.")
