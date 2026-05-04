import asyncio
from mcp.server.fastmcp import FastMCP
from pathlib import Path

# Create an MCP server configured to listen on all interfaces
mcp = FastMCP("solo-corp-os", host="0.0.0.0", port=8002)

@mcp.tool()
def search_wiki(query: str) -> str:
    """Search the Solo-Corp OS knowledge base for relevant context."""
    try:
        from app import select_relevant_wiki_pages, read_text_file
        pages = select_relevant_wiki_pages(query, limit=5)
        if not pages:
            return "No relevant wiki pages found."
        
        result = "Found the following relevant context:\n\n"
        for page in pages:
            content = read_text_file(page.path)[:4000] # Truncate slightly to fit context limits safely
            result += f"--- Page: {page.rel_path} ({page.title}) ---\n{content}\n\n"
        return result
    except Exception as e:
        return f"Error executing search: {str(e)}"

@mcp.tool()
def read_page(rel_path: str) -> str:
    """Read the full markdown content of a specific wiki page. Use the exact rel_path returned by search_wiki."""
    try:
        from app import WIKI_ROOT, read_text_file
        target_path = (WIKI_ROOT / rel_path).resolve()
        
        # Ensure safe path traversal
        if not target_path.is_relative_to(WIKI_ROOT.resolve()):
            return "Error: access denied."
            
        if not target_path.exists():
            return f"Error: file {rel_path} does not exist."
            
        content = read_text_file(target_path)
        return content
    except Exception as e:
        return f"Error reading file: {str(e)}"

if __name__ == "__main__":
    print("Starting Solo-Corp OS MCP Server on http://0.0.0.0:8002/sse ...")
    # You can choose 'sse' or 'streamable-http' as the transport based on OpenWebUI's config
    mcp.run(transport='streamable-http')
