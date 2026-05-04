import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field
import sys
from pathlib import Path

# Initialize FastAPI Server
app = FastAPI(
    title="Solo-Corp OS Tools", 
    description="Headless Knowledge Engine tools for Solo-Corp OS",
    version="1.0.0",
    # We dynamically set the server URL to whatever host runs this, assuming standard usage
    servers=[{"url": "http://192.168.0.226:8002"}] 
)

class SearchQuery(BaseModel):
    query: str

class ReadQuery(BaseModel):
    rel_path: str = Field(description="The exact relative path of the file you want to read.")

class IngestQuery(BaseModel):
    filename: str = Field(description="The name of the file to save, MUST include the .md extension.")
    content: str = Field(description="The ENTIRE RAW TEXT of the document. Do not summarize it. Do not just say 'full text'. You MUST copy and paste the entire raw text of the user's uploaded document here.")

@app.post("/api/search_wiki", description="Search the Solo-Corp OS knowledge base for relevant context. Returns markdown text of the most relevant documents.")
def search_wiki(request: SearchQuery) -> str:
    try:
        from app import select_relevant_wiki_pages, read_text_file
        pages = select_relevant_wiki_pages(request.query, limit=5)
        if not pages:
            return "No relevant wiki pages found."
        
        result = "Found the following relevant context:\n\n"
        for page in pages:
            content = read_text_file(page.path)[:4000] # Truncate slightly to fit context limits safely
            result += f"--- Page: {page.rel_path} ({page.title}) ---\n{content}\n\n"
        return result
    except Exception as e:
        return f"Error executing search: {str(e)}"

@app.post("/api/read_page", description="Read the full markdown content of a specific wiki page. Use the exact rel_path returned by search_wiki.")
def read_page(request: ReadQuery) -> str:
    try:
        from app import WIKI_ROOT, read_text_file
        target_path = (WIKI_ROOT / request.rel_path).resolve()
        
        # Ensure safe path traversal
        if not target_path.is_relative_to(WIKI_ROOT.resolve()):
            return "Error: access denied."
            
        if not target_path.exists():
            return f"Error: file {request.rel_path} does not exist."
            
        content = read_text_file(target_path)
        return content
    except Exception as e:
        return f"Error reading file: {str(e)}"

@app.post("/api/ingest_document", description="Save a new document or file to the Solo-Corp OS knowledge base, and trigger the background AI Wiki Builder to instantly process it into permanent memory.")
def ingest_document(request: IngestQuery) -> str:
    try:
        from app import RAW_SOURCES
        import urllib.request
        
        # Ensure filename is safe and has a markdown extension
        safe_name = "".join(c for c in request.filename if c.isalnum() or c in " ._-").strip()
        if not safe_name:
            safe_name = "uploaded_document"
            
        if not safe_name.endswith(".md") and not safe_name.endswith(".txt"):
            safe_name += ".md"
            
        target_path = RAW_SOURCES / safe_name
        target_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Save the file to raw/sources/
        target_path.write_text(request.content, encoding="utf-8")
        
        # Trigger the ingestion pipeline in app.py
        try:
            req = urllib.request.Request("http://127.0.0.1:8000/ingest-all", method="POST")
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            # We don't fail completely if the trigger fails, because the file is saved
            print(f"Warning: Failed to auto-trigger ingestion: {e}")
            
        return f"Successfully saved document as '{safe_name}'. The background Wiki Builder has been triggered to process it into the wiki."
    except Exception as e:
        return f"Error saving document: {str(e)}"

if __name__ == "__main__":
    print("=========================================================")
    print("Starting Solo-Corp OS Tool Server on port 8002...")
    print("Open WebUI OpenAPI URL: http://192.168.0.226:8002/openapi.json")
    print("=========================================================")
    uvicorn.run(app, host="0.0.0.0", port=8002)
