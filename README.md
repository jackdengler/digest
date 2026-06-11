# Readless — your personal daily digest

Readless pulls everything you'd normally skim — RSS feeds, Substacks, and email
newsletters — and turns it into **one readable daily digest** with a single LLM
call. The digest is published to a GitHub Pages site (your browsable archive)
and emailed to you every morning.

It runs **100% free**: GitHub Actions does the scheduling and compute, the
Gemini free tier does the summarizing, GitHub Pages does the hosting, and your
existing Gmail account handles newsletters in and the digest out.

```
RSS feeds ─┐
Substacks ─┼─► fetch & clean ─► dedupe ─► 1 LLM call ─┬─► site/digests/<date>.json ─► GitHub Pages
Gmail label┘   (last 26 hours)  (state.json)          └─► HTML email ─► your inbox
```

What the LLM does with each day's items:

- **Trending** — when 2+ distinct sources cover the same story, it synthesizes
  them into one insight instead of repeating each article.
- **Summaries** — 1–2 sentences per item (or 3–5 with `summary_style: detailed`),
  grouped by source, each with 1–3 topic tags.
- **Filtering** — pure ads/promos are skipped; your `topic_filters.exclude`
  keywords are dropped before the LLM ever sees them; `topic_filters.prioritize`
  topics get a ★ and sort to the top.
- **Dedupe** — an item is never digested twice (`state.json`, persisted via the
  Actions cache).

> **Note:** the repo ships with one *sample* digest (`site/digests/2026-06-10.json`)
> so the site renders before your first real run. Your first real digest will
> appear alongside it; feel free to delete the sample file afterwards.

---

## Setup (about 15 minutes)

### 1. Get a free Gemini API key

1. Go to [Google AI Studio](https://aistudio.google.com/app/apikey) and sign in.
2. Click **Create API key** and copy it. The free tier comfortably covers one
   digest run per day — cost: **$0**.

### 2. Create a Gmail App Password

This one password is used both to *read* your newsletter label (IMAP) and to
*send* the digest (SMTP).

1. Turn on **2-Step Verification** for your Google account if it isn't already:
   [myaccount.google.com/security](https://myaccount.google.com/security).
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Create an app password named e.g. `readless` and copy the 16-character code
   (ignore the spaces Google displays).

### 3. Add the three repository secrets

In this repo: **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name      | Value                                              |
| ---------------- | -------------------------------------------------- |
| `GEMINI_API_KEY` | the key from step 1                                |
| `IMAP_PASSWORD`  | the app password from step 2                       |
| `SMTP_PASSWORD`  | the same app password again                        |

(Optional: `ANTHROPIC_API_KEY` if you switch `provider: anthropic` later.)

### 4. Configure your email & sources

**Easiest: use the settings page on your digest site** (see
[The settings page](#the-settings-page-configure-everything-from-the-site)
below) once Pages is enabled — it has a form for your Gmail address,
recipients, feeds, Substacks, filters, and the daily run time.

Or edit `config.yaml` by hand; set at minimum:

```yaml
email:
  username: you@gmail.com   # your Gmail address (IMAP + SMTP + recipient)
site_url: https://<your-github-username>.github.io/digest/
```

Add or remove `rss_feeds` / `substacks` to taste — Substacks can be `"@handle"`,
a `substack.com/@handle` URL, or a publication URL.

### 5. Enable GitHub Pages

**Settings → Pages → Build and deployment → Source: GitHub Actions.**
That's the whole step — the workflow deploys the `site/` folder for you.

### 6. Trigger the first run

Go to the **Actions** tab → **Daily digest** → **Run workflow**. After a couple
of minutes you should have an email in your inbox and a fresh digest at your
Pages URL. From then on it runs automatically every day at **14:00 UTC** —
change the time from the site's settings page (Schedule section) or by editing
the cron in `.github/workflows/digest.yml`.

---

## The settings page (configure everything from the site)

Your Pages site has a **⚙ Settings** link (`…/settings.html`) where you can
change the Gmail account, who the digest is emailed to (comma-separate for
multiple recipients), the RSS feeds and Substacks, topic filters, summary
style, and the daily run time — no file editing needed. There's also a
**Run digest now** button.

Because GitHub Pages is a static site, saving works by committing `config.yaml`
(and, for schedule changes, the workflow file) back to this repository through
the GitHub API. That needs a **fine-grained personal access token**, created
once:

1. Open [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new).
2. *Repository access* → **Only select repositories** → pick this repo.
3. *Permissions → Repository permissions* → **Contents: Read and write**,
   **Actions: Read and write** (for the Run-now button), and
   **Workflows: Read and write** (for schedule changes).
4. Generate it, copy it, and paste it into the settings page.

The token is stored **only in your browser** (localStorage) — never in the
repository — so don't set it up on a shared computer, and use the page's
*Forget* button if you ever need to clear it. Anyone can *view* your public
site, but saving requires your token. Each save shows up as a normal commit
made by you.

---

## Email newsletters (the Gmail label)

Some of the best newsletters have no RSS feed. Subscribe with your Gmail
address and label them — Readless reads the label over IMAP.

**Worth subscribing to:** [TLDR](https://tldr.tech/), [Morning Brew](https://www.morningbrew.com/),
[The Hustle](https://thehustle.co/), [Axios AM](https://www.axios.com/newsletters/axios-am),
[Money Stuff](https://www.bloomberg.com/account/newsletters) (Matt Levine), and
[Politico Playbook](https://www.politico.com/playbook).

**Set up the label + filter in Gmail:**

1. Create a label called **Newsletters** (left sidebar → *More* → *Create new label*).
2. Click the search bar → *Show search options*, paste this into **From**, and
   choose *Create filter* → *Apply the label "Newsletters"*:

   ```
   from:(tldrnewsletter.com OR morningbrew.com OR thehustle.co OR axios.com OR mail.bloombergview.com OR politico.com)
   ```

3. (Optional) also tick *Skip the Inbox* so newsletters only reach you through
   the digest.

**Privacy note:** your Pages site is public, so by default
(`publish_email_items: false`) email-newsletter content appears **only in the
emailed digest**, never in the published JSON. The site's footer notes how many
items were email-only.

---

## Configuration reference

| Key                   | Default          | Meaning                                                       |
| --------------------- | ---------------- | ------------------------------------------------------------- |
| `lookback_hours`      | `26`             | only items newer than this are considered                     |
| `provider`            | `gemini`         | `gemini` \| `anthropic` \| `ollama`                           |
| `model`               | provider default | override the model name                                       |
| `summary_style`       | `concise`        | `concise` (1–2 sentences) or `detailed` (3–5 with key facts)  |
| `site_url`            | —                | your Pages URL, linked from the email                         |
| `publish_email_items` | `false`          | include email items in the public site JSON                   |
| `topic_filters.exclude`    | `[]`        | keyword list; matching items are dropped (counted in footer)  |
| `topic_filters.prioritize` | `[]`        | keyword list; matching items get a ★ and sort first           |
| `email.label`         | `Newsletters`    | Gmail label to read                                           |
| `email.mark_as_read`  | `false`          | mark digested emails as read                                  |
| `smtp.to`             | yourself         | recipient(s), comma-separated for multiple                    |

## LLM providers & cost

| Provider    | Model (default)     | Needs                       | Cost                                  |
| ----------- | ------------------- | --------------------------- | ------------------------------------- |
| `gemini`    | `gemini-2.5-flash`  | `GEMINI_API_KEY` secret     | **$0** (free tier)                    |
| `anthropic` | `claude-sonnet-4-5` | `ANTHROPIC_API_KEY` secret  | ≈ **$1–3/month** (a few ¢ per run)    |
| `ollama`    | `llama3.2`          | local Ollama at `ollama_url`| $0, local runs only (not in Actions)  |

Everything else — Actions minutes (public repo), Pages hosting, Gmail — is free.

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Dry run: fetches + cleans + (if a key is set) summarizes, writes a local
# digest_<date>.html preview. No email, no site files, no state update.
GEMINI_API_KEY=... python readless.py --dry-run

# Real run (writes site/, sends email, updates state.json):
GEMINI_API_KEY=... IMAP_PASSWORD=... SMTP_PASSWORD=... python readless.py
```

With no API key set, `--dry-run` still verifies fetching and cleaning and lists
what it *would* summarize — handy for testing new feeds.

Run the test suite (offline, no network needed):

```bash
pip install pytest
pytest
```

## How state & dedupe work

`readless.py` records a hash of `(source|title|url)` for every digested item in
`state.json` (the most recent 5,000). The workflow persists that file between
runs with the GitHub Actions cache. If the cache is ever evicted (GitHub drops
caches unused for ~7 days), the `lookback_hours` window still prevents old
items from flooding back in — at worst you might see yesterday's items once.

## Troubleshooting

- **Pages URL is 404** — make sure *Settings → Pages → Source* is **GitHub
  Actions**, then re-run the workflow once.
- **No email arrived** — check the workflow logs: a missing `SMTP_PASSWORD`
  logs a warning and skips sending (the site still updates). Also check spam
  for the first delivery.
- **`email: IMAP_PASSWORD not set, skipping`** — expected until you add the
  secret; RSS/Substack ingestion still works without it.
- **IMAP login fails** — app passwords require 2-Step Verification, and
  `email.username` must be the full Gmail address.
- **A feed keeps failing** — one bad source never blocks the digest; it logs a
  warning and the rest ship. Remove or replace the URL in `config.yaml`.
- **Empty digest** — "Nothing new since the last digest" is normal when no
  source published within the lookback window; nothing is sent that day.
