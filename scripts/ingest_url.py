#!/usr/bin/env python3
"""
Web Clipper for LLM Wiki

Fetches a URL, converts the HTML to Markdown using markdownify,
and saves it to the raw/sources directory.

Usage:
  python3 scripts/ingest_url.py <URL>
"""

import sys
import os
import re
import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md

def sanitize_filename(filename):
    return re.sub(r'[^\w\-_\. ]', '_', filename).strip().replace(' ', '-')

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/ingest_url.py <URL>")
        sys.exit(1)

    url = sys.argv[1]
    
    print(f"Fetching {url} ...")
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
    except Exception as e:
        print(f"Error fetching URL: {e}")
        sys.exit(1)

    soup = BeautifulSoup(response.content, 'html.parser')
    
    # Extract title
    title = soup.title.string if soup.title else "Untitled Article"
    title = title.strip()
    
    print(f"Title found: {title}")
    
    # Basic attempt to find the main content
    article = soup.find('article')
    if not article:
        article = soup.find('main')
    if not article:
        # Fallback to body if no semantic tags
        article = soup.body

    if not article:
        print("Could not parse the page content properly.")
        sys.exit(1)

    # Convert to markdown
    markdown_content = md(str(article), heading_style="ATX")
    
    # Output file
    filename = sanitize_filename(title) + ".md"
    output_path = os.path.join('raw', 'sources', filename)
    
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(f"# {title}\n\n")
        f.write(f"Source URL: {url}\n\n")
        f.write(markdown_content)
        
    print(f"Successfully saved to {output_path}")

if __name__ == '__main__':
    main()
