# AI Career Agent

Hi, I am **Mert Can Yalçın**, a Management Information Systems student in Istanbul.

I built this project because looking for internships every day takes a lot of time. This agent does the boring part for me. Every evening it finds new jobs, checks them, and sends them to my phone with a CV made for each job.

It is **free** and it runs **on your own computer**. You don't need a paid AI service.

---

## What it does

Every evening at 21:00 the agent:

1. **Finds new jobs** (internship / junior) in your city. It uses a free job API and web search.
2. **Checks each job.** It opens the job page and looks at the date, the city, the level and the experience they want. Old jobs, senior jobs and jobs in the wrong city are removed.
3. **Gives a match score** (0–100). It compares the job with your CV and tells you why it matches and what is missing.
4. **Makes a CV for that job.** It uses the same CV design, as Word and PDF. It only changes the order and the wording. **It never adds a skill or experience you don't have.**
5. **Writes a cover letter** for the job (Word and PDF).
6. **Sends everything to you on Telegram.**
7. **Never sends you the same job twice.**

On Telegram you see something like this:

```
NEW JOB OPPORTUNITY
Position: Data Analyst Intern
Company:  Example Company
Location: Istanbul (Hybrid)
MATCH SCORE: 64/100 (Good match)
WHY IT MATCHES:  - SQL  - Python  - Power BI
GAPS:            - Tableau
APPLY: https://...
[ ✅ Applied ]  [ 👎 Not interested ]

+ Your_Name_Company_CV.pdf / .docx
+ Your_Name_Company_Cover_Letter.pdf / .docx
```

- Tap **✅ Applied** or **👎 Not interested** to save your answer.
- Write **/status** to see your applications and deadlines.
- If you don't answer a job, the bot reminds you **3 days before the deadline**.

---

## Why it does NOT apply for you

**The agent never applies to jobs automatically. You always apply yourself.**

At first I thought about automatic applying. Then I decided not to build it, because:

- Sites like **LinkedIn do not allow bots** that apply or collect data automatically. It is against their rules, and they can **close your account**.
- A job application should come from **you**. You should read the job and check your CV before you send it.

For the same reason, the agent **does not scrape LinkedIn**. It only uses a job API and normal search results. It also respects `robots.txt`, it never logs in to any site, and it never tries to pass a CAPTCHA.

---

## What you need

- A **Windows 10 or 11** computer
- **Python 3.11 or newer**: [python.org](https://www.python.org/downloads/). During the install, tick **"Add python.exe to PATH"**.
- **Ollama**, the free local AI: [ollama.com](https://ollama.com). It needs about 4 GB of disk space.
- **Microsoft Word**, to make PDFs. It is optional: without Word you only get Word files.
- A **Telegram** account

---

## Installation (about 15 minutes)

### Step 1 – Download and run setup

Download this project (green **Code** button → **Download ZIP**, then unzip it). You can also use git:

```bat
git clone https://github.com/<user>/AI_Career_Agent.git
cd AI_Career_Agent
```

Then **double-click `setup.bat`**. It installs everything and creates three personal files for you:

| File | What it is |
|---|---|
| `.env` | your secret keys |
| `settings.json` | your city, search words and other settings |
| `CV\cv_content.json` | your CV |

These files stay on your computer. They are **never uploaded** to GitHub.

### Step 2 – Get a free job API key

1. Make an account on [rapidapi.com](https://rapidapi.com).
2. Search for **JSearch** and choose the free **Basic** plan (200 searches per month).
3. Copy your key and paste it into `.env`:
   ```
   RAPIDAPI_KEY=your_key_here
   ```

This step is optional, but you will find more jobs with the key. The agent stops before you use up the free limit.

### Step 3 – Make your Telegram bot

1. In Telegram, open **@BotFather** and write `/newbot`. Choose a name.
2. BotFather gives you a **token**. Paste it into `.env`:
   ```
   TELEGRAM_BOT_TOKEN=your_token_here
   ```
3. Open your new bot and write **/start**.

### Step 4 – Write your CV

Open `CV\cv_content.json` in a text editor (for example Notepad or VS Code). It has an example person, "Alex Demir". Replace every value with your own information.

**Only write things that are true.** The agent uses only what is in this file.

Want to see your CV? Run this:

```bat
.venv\Scripts\python.exe Python\cv_builder.py
```

It creates `CV\Template_Check.docx`.

### Step 5 – Check everything

```bat
.venv\Scripts\python.exe Python\setup_check.py --telegram
```

All lines should say **OK**. You also get a test message on Telegram. If a line says **FAIL**, it tells you how to fix it.

### Step 6 – Start the daily run and the bot

```bat
powershell -ExecutionPolicy Bypass -File schedule_tasks.ps1
```

That's it! This creates two Windows tasks:

- **AI Career Agent** – searches every evening at 21:00. To use another time, add it at the end: `... schedule_tasks.ps1 -Time 19:30`.
- **AI Career Agent Bot** – the Telegram bot. It runs in the background without a window, answers your buttons and `/status` immediately, and sends deadline reminders.

Your computer should be on (or in sleep mode) at 21:00. If it is off, the search runs when you turn it on.

---

## Try it now

You don't have to wait until 21:00:

| What | Command |
|---|---|
| Run the full daily search now | `run_daily.bat` |
| Test run (nothing is sent, nothing is saved) | `.venv\Scripts\python.exe Python\daily_pipeline.py --dry-run` |
| Test all steps again on a job you already got | `.venv\Scripts\python.exe Python\daily_pipeline.py --full-test` |
| Run the offline tests | `.venv\Scripts\python.exe Python\run_tests.py` |

Run all commands in the project folder.

---

## Change the settings

Open `settings.json`. The most useful settings:

| Setting | Default | Meaning |
|---|---|---|
| `city` | `Istanbul` | the city where you want to work |
| `job_api_queries` | data / BI / AI intern | what to search for |
| `role_keywords` | empty (= data / BI / AI / analyst) | your field, e.g. `["marketing", "social media"]` |
| `max_jobs_per_day` | `5` | the maximum number of jobs per evening |
| `max_age_days` | `7` | ignore jobs older than this |
| `deadline_reminder_days` | `3` | reminder before the deadline (`0` = off) |
| `cover_letter` | `true` | make a cover letter or not |
| `model` | `qwen3.5:4b` | the Ollama model. Bigger models write better, but they are slower. |

Example for a marketing student in Ankara:

```json
{
  "location": {"city": "Ankara"},
  "search": {"job_api_queries": ["digital marketing intern", "social media intern"]},
  "roles": {"role_keywords": ["marketing", "social media", "content"]}
}
```

If you delete a setting, the default is used.

---

## Please know this

- **Always read the CV and the cover letter before you send them.** The AI model is small, so it can sometimes write a wrong sentence. The agent checks for invented skills and numbers, but it cannot catch every mistake.
- **Location checks only know cities in Türkiye.**
- **The CV has one fixed design.** It is clean and ATS-friendly. Your own Word design is not copied.
- **The match score is only a guide.**
- **Some company websites cannot be read** (they need JavaScript). Those jobs are skipped or marked "check yourself".
- **The free job API gives about 6 searches per day.**

---

## Privacy

Everything runs on your computer. Only these things go out:

- the search words, to the job API and the search engine,
- the public job pages that the agent opens,
- the messages to **your own** Telegram bot.

Your CV, keys and job history stay in your files.

**Before you upload to GitHub,** run `git status`. Make sure `.env`, `settings.json`, `CV\cv_content.json`, `Database`, `Generated_CVs` and `Logs` are **not** in the list.

---

## Problems?

| Problem | Solution |
|---|---|
| "Ollama is not reachable" | Open the Ollama app, then run `ollama pull qwen3.5:4b` |
| No Telegram messages | Write `/start` to your bot, then run `setup_check.py --telegram` |
| Buttons or `/status` don't answer | Run `schedule_tasks.ps1` again. You can also look at `Logs\bot.log` |
| `.env` is not found | Windows may have saved it as `.env.txt`. Rename it: `Rename-Item .env.txt .env` |
| Only Word files, no PDF | Install Microsoft Word or LibreOffice |
| Nothing came at 21:00 | Look at `Logs\daily_run.log` |

---

## How it works (for developers)

```
job API + web search ─> job_search ─> job_page_inspector ─> match_scorer ─> cv_tailor + cover_letter ─> Telegram
                         (filters)     (reads the real page)  (score)        (Word + PDF)
                                         job_store (SQLite): no duplicates, your answers, reminders
```

- The local AI (Qwen on Ollama) **only makes suggestions**. Python code checks every suggestion: no new skills, no new numbers, no stronger claims. If a suggestion fails the check, the original sentence is kept.
- The agent does not open private or local network addresses (SSRF protection).
- There are about 120 offline tests: `Python\run_tests.py`.

---

## Author

**Mert Can Yalçın** – Management Information Systems student, İstinye University, Istanbul

If you find a problem or have an idea, please open an issue.

## License

MIT, see [LICENSE](LICENSE). You can use it and change it freely. Please follow the rules of each job site.
