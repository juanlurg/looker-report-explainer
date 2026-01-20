#!/usr/bin/env python3
"""
Looker Studio Report Describer

Processes a CSV of Looker Studio reports, captures screenshots and HTML,
and generates detailed descriptions using Gemini 2.5 Flash via Vertex AI.
"""

import argparse
import asyncio
import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import vertexai
from dotenv import load_dotenv
from vertexai.generative_models import GenerativeModel, Image
from playwright.async_api import async_playwright

# Load environment variables from .env file
load_dotenv()

import os

# Configuration from environment
VERTEX_PROJECT_ID = os.getenv("VERTEX_PROJECT_ID")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
LOOKER_STUDIO_URL = os.getenv("LOOKER_STUDIO_URL", "https://lookerstudio.google.com")

AUTH_STATE_FILE = "auth_state.json"
OUTPUT_DIR = "output"
DEFAULT_TIMEOUT = 60000  # 60 seconds for page loads


@dataclass
class PageCapture:
    """Represents a captured page from a Looker Studio report."""
    page_number: int
    page_name: str
    screenshot_path: Path
    html_path: Path
    html_content: str


GEMINI_PROMPT_SINGLE = """You are analyzing a Looker Studio dashboard/report. Based on the provided information, write a detailed description of this report.

**Report Name:** {name}

**Initial Description:** {description}

**Page HTML:** (provided below)

**Screenshot:** (provided as image)

Please provide a comprehensive description that includes:
1. The purpose and main function of this report
2. Key metrics, KPIs, or data points displayed
3. Any filters, date ranges, or parameters visible
4. The types of visualizations used (charts, tables, etc.)
5. Who would likely use this report and for what decisions
6. Any notable features or sections of the dashboard

Write the description in clear, professional language suitable for documentation.
"""

GEMINI_PROMPT_MULTI = """You are analyzing a multi-page Looker Studio dashboard/report. Based on the provided information, write a detailed description covering ALL pages of this report.

**Report Name:** {name}

**Initial Description:** {description}

**Number of Pages:** {page_count}

**Page Names:** {page_names}

**Page HTML:** (provided below for each page)

**Screenshots:** (provided as images for each page)

Please provide a comprehensive description that includes:
1. The overall purpose and main function of this report
2. A summary of what each page contains and its role in the overall report
3. Key metrics, KPIs, or data points displayed across all pages
4. Any filters, date ranges, or parameters visible
5. The types of visualizations used (charts, tables, etc.)
6. Who would likely use this report and for what decisions
7. How the pages relate to each other and the overall narrative/flow

Write the description in clear, professional language suitable for documentation.
"""


def sanitize_filename(name: str) -> str:
    """Convert report name to safe filename."""
    safe = re.sub(r'[<>:"/\\|?*]', '_', name)
    safe = re.sub(r'\s+', '_', safe)
    safe = safe.strip('_')
    return safe[:100]


async def extract_body_html(page) -> str:
    """Extract only body innerHTML, excluding scripts, styles, and noscript tags."""
    return await page.evaluate("""
        () => {
            const bodyClone = document.body.cloneNode(true);
            bodyClone.querySelectorAll('script').forEach(el => el.remove());
            bodyClone.querySelectorAll('style').forEach(el => el.remove());
            bodyClone.querySelectorAll('noscript').forEach(el => el.remove());
            return bodyClone.innerHTML;
        }
    """)


async def save_auth_state(page, auth_file: str):
    """Save browser authentication state to file."""
    await page.context.storage_state(path=auth_file)
    print(f"Authentication state saved to {auth_file}")


async def wait_for_looker_studio_load(page, timeout: int = DEFAULT_TIMEOUT):
    """Wait for Looker Studio dashboard to finish loading."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)

        # Looker Studio specific loading selectors
        loading_selectors = [
            "[data-loading='true']",
            ".loading-spinner",
            "[data-testid='loading']",
            "[aria-busy='true']",
            ".lsapp-loading-indicator",
            "[class*='loading']",
        ]

        for selector in loading_selectors:
            try:
                await page.wait_for_selector(
                    selector,
                    state="hidden",
                    timeout=5000
                )
            except:
                pass

        await asyncio.sleep(2)

    except Exception as e:
        print(f"Warning: Page load wait encountered issue: {e}")


async def detect_report_pages(page) -> List[dict]:
    """Detect multi-page reports using multiple selector strategies.

    Returns list of page info dicts with 'name' and 'selector' keys,
    or empty list for single-page reports.
    """
    pages = []

    # Strategy 1: Tab navigation (common in Looker Studio)
    tab_selectors = [
        '[role="tablist"] [role="tab"]',
        '[data-testid="page-tab"]',
        '.page-tab',
    ]

    for selector in tab_selectors:
        try:
            tabs = await page.query_selector_all(selector)
            if len(tabs) > 1:
                for i, tab in enumerate(tabs):
                    name = await tab.inner_text()
                    name = name.strip() if name else f"Page {i + 1}"
                    pages.append({
                        "name": name,
                        "selector": selector,
                        "index": i
                    })
                if pages:
                    return pages
        except:
            pass

    # Strategy 2: Page navigation sidebar
    nav_selectors = [
        'nav [role="button"]',
        '[class*="page-nav"] [role="button"]',
        '[class*="page-list"] [role="button"]',
        '[data-testid="page-navigator"] [role="button"]',
    ]

    for selector in nav_selectors:
        try:
            buttons = await page.query_selector_all(selector)
            if len(buttons) > 1:
                for i, btn in enumerate(buttons):
                    name = await btn.inner_text()
                    name = name.strip() if name else f"Page {i + 1}"
                    pages.append({
                        "name": name,
                        "selector": selector,
                        "index": i
                    })
                if pages:
                    return pages
        except:
            pass

    # Strategy 3: Look for any element with "page" in class containing multiple items
    try:
        page_elements = await page.query_selector_all('[class*="page"][role="button"], [class*="Page"][role="button"]')
        if len(page_elements) > 1:
            for i, el in enumerate(page_elements):
                name = await el.inner_text()
                name = name.strip() if name else f"Page {i + 1}"
                pages.append({
                    "name": name,
                    "element": el,
                    "index": i
                })
            if pages:
                return pages
    except:
        pass

    return pages


async def navigate_to_page(page, page_info: dict) -> bool:
    """Click on page navigation element and wait for load.

    Returns True if navigation succeeded, False otherwise.
    """
    try:
        if "element" in page_info:
            # Direct element reference
            await page_info["element"].click()
        else:
            # Use selector + index
            elements = await page.query_selector_all(page_info["selector"])
            if page_info["index"] < len(elements):
                await elements[page_info["index"]].click()
            else:
                return False

        # Wait for page content to update
        await asyncio.sleep(1)
        await wait_for_looker_studio_load(page, timeout=30000)
        return True
    except Exception as e:
        print(f"    Warning: Failed to navigate to page: {e}")
        return False


async def capture_single_page(
    page,
    output_path: Path,
    safe_name: str,
    page_number: int,
    page_name: str,
    is_multi_page: bool
) -> PageCapture:
    """Capture screenshot and clean body HTML for one page."""
    # Determine file naming
    if is_multi_page:
        file_base = f"{safe_name}_page{page_number}"
    else:
        file_base = safe_name

    # Capture screenshot
    screenshot_path = output_path / f"{file_base}.png"
    await page.screenshot(path=str(screenshot_path), full_page=True)

    # Extract clean body HTML
    html_content = await extract_body_html(page)
    html_path = output_path / f"{file_base}.html"
    html_path.write_text(html_content, encoding="utf-8")

    return PageCapture(
        page_number=page_number,
        page_name=page_name,
        screenshot_path=screenshot_path,
        html_path=html_path,
        html_content=html_content
    )


async def capture_report(page, url: str, output_path: Path, name: str) -> List[PageCapture]:
    """Navigate to report and capture screenshot + HTML for all pages."""
    print(f"  Navigating to: {url}")
    await page.goto(url, wait_until="domcontentloaded")

    print("  Waiting for dashboard to load...")
    await wait_for_looker_studio_load(page)

    safe_name = sanitize_filename(name)
    captures: List[PageCapture] = []

    # Detect if this is a multi-page report
    detected_pages = await detect_report_pages(page)

    if not detected_pages:
        # Single-page report
        print("  Single-page report detected")
        capture = await capture_single_page(
            page, output_path, safe_name,
            page_number=1,
            page_name="Main",
            is_multi_page=False
        )
        captures.append(capture)
        print(f"  Screenshot saved: {capture.screenshot_path}")
        print(f"  HTML saved: {capture.html_path}")
    else:
        # Multi-page report
        print(f"  Multi-page report detected: {len(detected_pages)} pages")

        for i, page_info in enumerate(detected_pages):
            page_num = i + 1
            page_name = page_info.get("name", f"Page {page_num}")
            print(f"    Capturing page {page_num}/{len(detected_pages)}: {page_name}")

            # Navigate to page (first page is already loaded)
            if i > 0:
                success = await navigate_to_page(page, page_info)
                if not success:
                    print(f"    Skipping page {page_num} - navigation failed")
                    continue

            capture = await capture_single_page(
                page, output_path, safe_name,
                page_number=page_num,
                page_name=page_name,
                is_multi_page=True
            )
            captures.append(capture)
            print(f"    Saved: {capture.screenshot_path.name}, {capture.html_path.name}")

    return captures


def generate_description(
    name: str,
    initial_description: str,
    captures: List[PageCapture],
    model: GenerativeModel
) -> str:
    """Generate detailed description using Gemini via Vertex AI."""
    is_multi_page = len(captures) > 1

    if is_multi_page:
        # Multi-page report
        page_names = ", ".join(c.page_name for c in captures)
        prompt = GEMINI_PROMPT_MULTI.format(
            name=name,
            description=initial_description,
            page_count=len(captures),
            page_names=page_names
        )

        # Scale HTML limit by page count (50k base, divided among pages)
        max_html_per_page = 50000 // len(captures)

        # Build HTML content for all pages
        html_sections = []
        for capture in captures:
            html = capture.html_content
            if len(html) > max_html_per_page:
                html = html[:max_html_per_page] + "\n... [HTML truncated]"
            html_sections.append(
                f"### Page {capture.page_number}: {capture.page_name}\n```html\n{html}\n```"
            )

        full_prompt = f"{prompt}\n\n---\n\n**HTML Content:**\n\n" + "\n\n".join(html_sections)

        # Load all images for Vertex AI
        images = [Image.load_from_file(str(c.screenshot_path)) for c in captures]
        content = [full_prompt] + images
    else:
        # Single-page report
        prompt = GEMINI_PROMPT_SINGLE.format(
            name=name,
            description=initial_description
        )

        html_content = captures[0].html_content
        max_html_chars = 50000
        if len(html_content) > max_html_chars:
            html_content = html_content[:max_html_chars] + "\n... [HTML truncated]"

        full_prompt = f"{prompt}\n\n---\n\n**HTML Content:**\n```html\n{html_content}\n```"

        # Load image for Vertex AI
        image = Image.load_from_file(str(captures[0].screenshot_path))
        content = [full_prompt, image]

    response = model.generate_content(content)
    return response.text


async def run_auth_flow(playwright, looker_studio_url: str):
    """Run interactive authentication flow."""
    print("\n=== Authentication Required ===")
    print("A browser window will open. Please log in to Looker Studio with your Google account.")
    print("After successful login, press Enter in this terminal to continue...")

    browser = await playwright.chromium.launch(headless=False)
    context = await browser.new_context()
    page = await context.new_page()

    if not looker_studio_url:
        looker_studio_url = input("Enter your Looker Studio URL (e.g., https://lookerstudio.google.com): ").strip()

    print(f"Opening: {looker_studio_url}")
    await page.goto(looker_studio_url)

    input("\nPress Enter after you have successfully logged in...")

    await save_auth_state(page, AUTH_STATE_FILE)
    await browser.close()
    print("Authentication complete!")


async def process_reports(csv_path: str):
    """Main processing function."""
    # Validate configuration
    if not VERTEX_PROJECT_ID:
        print("Error: VERTEX_PROJECT_ID not set. Check your .env file.")
        return

    # Initialize Vertex AI
    print(f"Initializing Vertex AI (project: {VERTEX_PROJECT_ID}, location: {VERTEX_LOCATION})")
    vertexai.init(project=VERTEX_PROJECT_ID, location=VERTEX_LOCATION)
    model = GenerativeModel("gemini-2.5-flash-preview-05-20")

    output_path = Path(OUTPUT_DIR)
    output_path.mkdir(exist_ok=True)

    reports = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            reports.append({
                "name": row.get("name", "").strip(),
                "url": row.get("url", "").strip(),
                "description": row.get("description", "").strip()
            })

    print(f"Found {len(reports)} reports to process")

    async with async_playwright() as playwright:
        if not Path(AUTH_STATE_FILE).exists():
            await run_auth_flow(playwright, LOOKER_STUDIO_URL)

        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=AUTH_STATE_FILE)
        page = await context.new_page()

        for i, report in enumerate(reports, 1):
            name = report["name"]
            url = report["url"]
            description = report["description"]

            print(f"\n[{i}/{len(reports)}] Processing: {name}")

            if not url:
                print("  Skipping - no URL provided")
                continue

            try:
                captures = await capture_report(page, url, output_path, name)

                if not captures:
                    print("  No pages captured, skipping")
                    continue

                print("  Generating description with Gemini...")
                detailed_description = generate_description(
                    name, description, captures, model
                )

                safe_name = sanitize_filename(name)
                desc_path = output_path / f"{safe_name}.txt"
                desc_path.write_text(detailed_description, encoding="utf-8")
                print(f"  Description saved: {desc_path}")

            except Exception as e:
                print(f"  ERROR: {e}")
                continue

        await browser.close()

    print(f"\n=== Complete! Output saved to {OUTPUT_DIR}/ ===")


def main():
    parser = argparse.ArgumentParser(
        description="Generate detailed descriptions for Looker Studio reports using Gemini via Vertex AI"
    )
    parser.add_argument(
        "csv_file",
        help="Path to CSV file with columns: name, url, description"
    )
    parser.add_argument(
        "--reauth",
        action="store_true",
        help="Force re-authentication even if auth state exists"
    )

    args = parser.parse_args()

    if args.reauth and Path(AUTH_STATE_FILE).exists():
        Path(AUTH_STATE_FILE).unlink()
        print("Removed existing auth state")

    asyncio.run(process_reports(args.csv_file))
    return 0


if __name__ == "__main__":
    exit(main())
