# Looker Studio Report Explainer

Automatically generates detailed descriptions for Looker Studio reports using Gemini 2.5 Flash via Vertex AI.

Given a CSV list of Looker Studio reports, this tool:
1. Opens each report in a browser (with saved Google authentication)
2. Waits for dashboards to fully load
3. Detects multi-page reports and iterates through all pages
4. Captures a screenshot and clean HTML (body only, no scripts/styles) for each page
5. Sends everything to Gemini to generate a detailed description covering all pages

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) package manager
- Google Cloud project with Vertex AI API enabled
- Access to Looker Studio reports (lookerstudio.google.com)

## Setup

### 1. Install dependencies

```bash
uv sync
uv run playwright install chromium
```

### 2. Configure environment

Copy the example environment file and edit it:

```bash
cp .env.example .env
```

Edit `.env` with your values:

```env
VERTEX_PROJECT_ID=your-gcp-project-id
VERTEX_LOCATION=us-central1
LOOKER_STUDIO_URL=https://lookerstudio.google.com
```

### 3. Authenticate with Google Cloud

Option A - Use your Google account (recommended for local development):

```bash
gcloud auth application-default login
```

Option B - Use a service account:

```bash
# Download service account key, then add to .env:
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account-key.json
```

## Usage

### Prepare your CSV file

Create a CSV file with your Looker Studio reports. Required columns: `name`, `url`, `description`

Example `reports.csv`:

```csv
name,url,description
Sales Dashboard,https://lookerstudio.google.com/reporting/abc123,Overview of sales metrics
Marketing Funnel,https://lookerstudio.google.com/reporting/def456,Marketing conversion funnel
Weekly KPIs,https://lookerstudio.google.com/reporting/ghi789,Weekly key performance indicators
```

### Run the tool

```bash
uv run python looker_describer.py reports.csv
```

### First run - Looker Studio authentication

On the first run (or if `auth_state.json` doesn't exist):

1. A browser window opens automatically
2. You'll be directed to Looker Studio
3. Log in with your Google account
4. Once logged in, return to the terminal and press Enter
5. Your session is saved to `auth_state.json` for future runs

Subsequent runs will use the saved session and run headlessly.

### Force re-authentication

If your Looker Studio session expires:

```bash
uv run python looker_describer.py reports.csv --reauth
```

## Output

All output is saved to the `output/` directory.

**Single-page reports:**
```
output/
├── Sales_Dashboard.txt    # Generated description
├── Sales_Dashboard.png    # Full-page screenshot
├── Sales_Dashboard.html   # Clean body HTML (no scripts/styles)
└── ...
```

**Multi-page reports:**
```
output/
├── Marketing_Report_page1.png    # Screenshot for page 1
├── Marketing_Report_page1.html   # Clean HTML for page 1
├── Marketing_Report_page2.png    # Screenshot for page 2
├── Marketing_Report_page2.html   # Clean HTML for page 2
├── Marketing_Report.txt          # Aggregated description for all pages
└── ...
```

The HTML files contain only the body content with scripts, styles, and noscript tags removed for cleaner analysis.

## Customizing the prompt

Edit the prompt templates at the top of `looker_describer.py` to customize what Gemini generates:
- `GEMINI_PROMPT_SINGLE` - Used for single-page reports
- `GEMINI_PROMPT_MULTI` - Used for multi-page reports (includes page count and names)

## Troubleshooting

### "VERTEX_PROJECT_ID not set"

Make sure your `.env` file exists and contains `VERTEX_PROJECT_ID=your-project-id`

### Authentication errors with Vertex AI

Run `gcloud auth application-default login` and make sure you have access to the project.

### Looker Studio pages not loading correctly

- Try increasing `DEFAULT_TIMEOUT` in the script (default: 60 seconds)
- Use `--reauth` to get a fresh Looker Studio session
- Make sure you have access to the reports you're trying to capture

### Screenshots are blank or incomplete

Some dashboards take longer to load. Increase the `asyncio.sleep(2)` in `wait_for_looker_studio_load()` to give more time for charts to render.
