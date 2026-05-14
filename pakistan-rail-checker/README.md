# 🚂 Pakistan Railway Seat Checker (GitHub Actions)

Automatically checks Pakistan Railway e-ticketing every **15 minutes** and sends a **Gmail alert** the moment Economy seats become available.

| Setting | Value |
|---|---|
| **Route** | Karachi → Sadikabad |
| **Travel Date** | 2026-05-23 |
| **Class** | Economy |
| **Trains** | Khyber Mail, Fareed Express, Bahauddin Zakria Express |
| **Alert Email** | hr677241@gmail.com |

---

## 📁 Files

```
pakistan-rail-checker/
├── .github/
│   └── workflows/
│       └── check_seats.yml   ← GitHub Actions scheduler
├── check_seats.py            ← Main seat-checking script
├── requirements.txt          ← Python packages
└── README.md
```

---

## 🚀 Setup (Step-by-Step)

### Step 1 — Create a GitHub repository

1. Go to [github.com](https://github.com) → **New repository**
2. Name it `pakistan-rail-checker`
3. Set it to **Private** (so your email stays private)
4. Click **Create repository**
5. Upload all these files into it

---

### Step 2 — Get a Gmail App Password

> Gmail blocks normal password logins for scripts. You need an **App Password**.

1. Go to your Google Account → **Security**
2. Make sure **2-Step Verification** is turned ON
3. Search for **"App Passwords"** at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
4. Choose:
   - App: **Mail**
   - Device: **Other** → type `RailChecker`
5. Click **Generate** → copy the 16-character password shown (e.g. `abcd efgh ijkl mnop`)

---

### Step 3 — Add GitHub Secrets

1. In your GitHub repo → **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret** and add these two:

| Secret Name | Value |
|---|---|
| `GMAIL_USER` | Your Gmail address (e.g. `hr677241@gmail.com`) |
| `GMAIL_APP_PASSWORD` | The 16-char App Password from Step 2 (no spaces) |

---

### Step 4 — Enable GitHub Actions

1. In your repo → click the **Actions** tab
2. If prompted, click **"I understand my workflows, go ahead and enable them"**
3. You'll see **"Pakistan Railway Seat Checker"** listed
4. Click **Run workflow** → **Run workflow** to test it immediately

---

### Step 5 — Check it's working

- Go to **Actions** tab → click the latest run
- Look for green ✅ (no seats found, ran OK) or check your email for an alert
- If it fails ❌, click the run → expand the step to see the error

---

## 📧 What the Email Looks Like

When seats are found you'll get an HTML email with:
- Train name
- Number of Economy seats available
- A **Book Now** link to the Pakistan Railway site

If the checker itself crashes, you'll get a separate ⚠️ error email.

---

## ⚙️ Customising

Open `check_seats.py` and edit the top section:

```python
TRAVEL_DATE   = "2026-05-23"    # Change the date
FROM_CODE     = "KCI"           # Karachi City station code
TO_CODE       = "SDB"           # Sadikabad station code
TARGET_TRAINS = [               # Add/remove trains (lowercase)
    "khyber mail",
    "fareed express",
    "bahauddin zakria express",
]
TARGET_CLASS  = "economy"       # Change class if needed
```

To change check frequency, edit `.github/workflows/check_seats.yml`:
```yaml
- cron: "*/15 * * * *"   # every 15 min
- cron: "*/30 * * * *"   # every 30 min
- cron: "0 * * * *"      # every hour
```

---

## ⚠️ Notes

- GitHub Actions **free tier** gives 2,000 minutes/month. At 15-min checks this job runs ≈ 96 times/day × ~1 min each = ~96 min/day → well within free limits.
- GitHub's cron scheduler may run a few minutes late during peak hours — this is normal.
- If the Pakistan Railway website changes its HTML structure the parser may need updating. Check the Actions log for errors.

---

## 🛑 Stopping the Checker

Go to **Actions** → click **"Pakistan Railway Seat Checker"** → **"..."** → **Disable workflow**.
