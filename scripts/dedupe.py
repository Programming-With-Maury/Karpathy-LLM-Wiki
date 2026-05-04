import os
import json
import re
import textwrap
import urllib.parse
from pathlib import Path
import requests

ROOT = Path(__file__).parent.parent.resolve()
WIKI_ROOT = ROOT / "wiki"
ENV_PATH = ROOT / ".env"

def load_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values

def get_vertex_config():
    dotenv = load_dotenv(ENV_PATH)
    api_key = os.getenv("VERTEX_API", dotenv.get("VERTEX_API", ""))
    model_id = os.getenv("MODEL_ID", dotenv.get("MODEL_ID", ""))
    return api_key, model_id

def vertex_request_json(payload, label=""):
    api_key, model_id = get_vertex_config()
    url = f"https://aiplatform.googleapis.com/v1/publishers/google/models/{urllib.parse.quote(model_id)}:generateContent?key={urllib.parse.quote(api_key)}"
    print(f"Calling Vertex AI: {label}")
    resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=180)
    resp.raise_for_status()
    body = resp.json()
    try:
        text = body["candidates"][0]["content"]["parts"][0]["text"]
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|```$", "", cleaned, flags=re.DOTALL).strip()
        return json.loads(cleaned)
    except Exception as e:
        print(f"Failed to parse JSON response: {e}")
        return None

def vertex_request_text(payload, label=""):
    api_key, model_id = get_vertex_config()
    url = f"https://aiplatform.googleapis.com/v1/publishers/google/models/{urllib.parse.quote(model_id)}:generateContent?key={urllib.parse.quote(api_key)}"
    print(f"Calling Vertex AI: {label}")
    resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=180)
    resp.raise_for_status()
    body = resp.json()
    try:
        text = body["candidates"][0]["content"]["parts"][0]["text"]
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:markdown|md)?\s*|```$", "", cleaned, flags=re.DOTALL).strip()
        return cleaned
    except Exception as e:
        print(f"Failed to parse text response: {e}")
        return None

def process_directory(dir_name):
    dir_path = WIKI_ROOT / dir_name
    if not dir_path.exists():
        return

    items = []
    for file_path in dir_path.glob("*.md"):
        content = file_path.read_text(encoding="utf-8")
        title_match = re.search(r"^#\s+(.+)$", content, flags=re.MULTILINE)
        summary_match = re.search(r"## Summary\n+(.+?)(?=\n##|$)", content, flags=re.DOTALL)
        
        title = title_match.group(1).strip() if title_match else file_path.stem
        summary = summary_match.group(1).strip() if summary_match else ""
        
        items.append({
            "path": file_path.name,
            "title": title,
            "summary": summary
        })

    if not items:
        return

    prompt = textwrap.dedent(f"""
    You are managing a highly structured wiki. Review the following list of files in the `{dir_name}` directory.
    Your goal is to identify clusters of duplicate or highly overlapping items (e.g. plurals vs singulars like "creator" and "creators", or slight variations like "shivam-maurya" and "shivam").
    
    Return a JSON object with a single key "clusters", which is a list of clusters.
    Each cluster should be an object with:
    - "canonical_path": The filename that should be kept (choose the best, most comprehensive or standard name among the duplicates).
    - "duplicate_paths": A list of filenames that mean the exact same thing and should be merged into the canonical_path.
    
    ONLY include clusters where there are actual duplicates to merge. Do not include items that are unique.
    
    Files:
    {json.dumps(items, indent=2, ensure_ascii=False)}
    """)

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
    }

    result = vertex_request_json(payload, f"Identify duplicates in {dir_name}")
    if not result or "clusters" not in result:
        print(f"No clusters found in {dir_name}.")
        return

    for cluster in result.get("clusters", []):
        canonical = cluster.get("canonical_path")
        duplicates = [d for d in cluster.get("duplicate_paths", []) if d != canonical]
        
        if not canonical or not duplicates:
            continue
            
        print(f"Found cluster: {canonical} <- {duplicates}")
        
        canonical_file = dir_path / canonical
        if not canonical_file.exists():
            continue
            
        merged_contents = []
        merged_contents.append(f"--- CANONICAL FILE: {canonical} ---\n" + canonical_file.read_text(encoding="utf-8"))
        
        for dup in duplicates:
            dup_file = dir_path / dup
            if dup_file.exists():
                merged_contents.append(f"--- DUPLICATE FILE TO MERGE: {dup} ---\n" + dup_file.read_text(encoding="utf-8"))
                
        merge_prompt = textwrap.dedent(f"""
        You are maintaining a markdown wiki. You need to merge several duplicate files into a single, cohesive canonical file.
        
        Combine the information from the DUPLICATE files into the CANONICAL file.
        - Preserve all unique Key Points, Links, and Sources.
        - Remove repetitive or identical points.
        - Keep the exact markdown structure (Summary, Key Points, Evidence / Notes, Links, Sources, Open Questions).
        - DO NOT invent new facts or links not present in the provided files.
        - ONLY return the final markdown content for the canonical file.
        
        Files to merge:
        {"".join(merged_contents)}
        """)
        
        merge_payload = {
            "contents": [{"role": "user", "parts": [{"text": merge_prompt}]}],
            "generationConfig": {"temperature": 0.1}
        }
        
        merged_markdown = vertex_request_text(merge_payload, f"Merge into {canonical}")
        if merged_markdown:
            canonical_file.write_text(merged_markdown, encoding="utf-8")
            print(f"Updated canonical file {canonical}")
            
            # Delete duplicates and remove from index.md
            index_path = WIKI_ROOT / "index.md"
            if index_path.exists():
                index_text = index_path.read_text(encoding="utf-8")
                
            for dup in duplicates:
                dup_file = dir_path / dup
                if dup_file.exists():
                    dup_file.unlink()
                    print(f"Deleted {dup}")
                    
                # Clean up index
                rel_dup = f"{dir_name}/{dup}"
                if index_path.exists():
                    index_lines = [line for line in index_text.splitlines() if f"({rel_dup})" not in line]
                    index_text = "\n".join(index_lines)
            
            if index_path.exists():
                index_path.write_text(index_text, encoding="utf-8")

if __name__ == "__main__":
    print("Starting deduplication process...")
    process_directory("entities")
    process_directory("concepts")
    print("Deduplication complete. Run `python3 scripts/lint.py` to fix any dangling links in other pages.")
