#!/usr/bin/env python3
"""
Looker Report Describer

Processes a CSV of Looker reports, captures screenshots and HTML,
and generates detailed descriptions using Gemini 2.5 Flash via Vertex AI.
"""

import argparse
import asyncio
import csv
import re
from pathlib import Path

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
LOOKER_BASE_URL = os.getenv("LOOKER_BASE_URL")

AUTH_STATE_FILE = "auth_state.json"
OUTPUT_DIR = "output"
DEFAULT_TIMEOUT = 60000  # 60 seconds for page loads

GEMINI_PROMPT = """You are analyzing a Looker dashboard/report. Based on the provided information, write a detailed description of this report.

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


def sanitize_filename(name: str) -> str:
    """Convert report name to safe filename."""
    safe = re.sub(r'[<>:"/\\|?*]', '_', name)
    safe = re.sub(r'\s+', '_', safe)
    safe = safe.strip('_')
    return safe[:100]


async def save_auth_state(page, auth_file: str):
    """Save browser authentication state to file."""
    await page.context.storage_state(path=auth_file)
    print(f"Authentication state saved to {auth_file}")


async def wait_for_looker_load(page, timeout: int = DEFAULT_TIMEOUT):
    """Wait for Looker dashboard to finish loading."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)

        loading_selectors = [
            ".lk-loading",
            ".loading-spinner",
            "[data-testid='loading']",
            ".dashboard-loading",
            ".viz-loading",
            "lk-spinner",
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


async def capture_report(page, url: str, output_path: Path, name: str):
    """Navigate to report and capture screenshot + HTML."""
    print(f"  Navigating to: {url}")
    await page.goto(url, wait_until="domcontentloaded")

    print("  Waiting for dashboard to load...")
    await wait_for_looker_load(page)

    safe_name = sanitize_filename(name)

    screenshot_path = output_path / f"{safe_name}.png"
    await page.screenshot(path=str(screenshot_path), full_page=True)
    print(f"  Screenshot saved: {screenshot_path}")

    html_content = await page.content()
    html_path = output_path / f"{safe_name}.html"
    html_path.write_text(html_content, encoding="utf-8")
    print(f"  HTML saved: {html_path}")

    return screenshot_path, html_path, html_content


def generate_description(
    name: str,
    initial_description: str,
    html_content: str,
    screenshot_path: Path,
    model: GenerativeModel
) -> str:
    """Generate detailed description using Gemini via Vertex AI."""
    prompt = GEMINI_PROMPT.format(
        name=name,
        description=initial_description
    )

    max_html_chars = 50000
    if len(html_content) > max_html_chars:
        html_content = html_content[:max_html_chars] + "\n... [HTML truncated]"

    full_prompt = f"{prompt}\n\n---\n\n**HTML Content:**\n```html\n{html_content}\n```"

    # Load image for Vertex AI
    image = Image.load_from_file(str(screenshot_path))

    response = model.generate_content([full_prompt, image])
    return response.text


async def run_auth_flow(playwright, looker_url: str):
    """Run interactive authentication flow."""
    print("\n=== Authentication Required ===")
    print("A browser window will open. Please log in to Looker.")
    print("After successful login, press Enter in this terminal to continue...")

    browser = await playwright.chromium.launch(headless=False)
    context = await browser.new_context()
    page = await context.new_page()

    if not looker_url:
        looker_url = input("Enter your Looker base URL (e.g., https://company.looker.com): ").strip()

    print(f"Opening: {looker_url}")
    await page.goto(looker_url)

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
            await run_auth_flow(playwright, LOOKER_BASE_URL)

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
                screenshot_path, html_path, html_content = await capture_report(
                    page, url, output_path, name
                )

                print("  Generating description with Gemini...")
                detailed_description = generate_description(
                    name, description, html_content, screenshot_path, model
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
        description="Generate detailed descriptions for Looker reports using Gemini via Vertex AI"
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
