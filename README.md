# Slack Chatbot — Ask Questions About Your Slack Messages

> **Live App:** https://d2586s68tp0zxo.cloudfront.net/

This tool lets you search and ask AI-powered questions about your Slack message history — right from your browser. No coding needed to use it.

---

## What Does It Do?

- **Connect** your Slack workspace with one click
- **Search** through your Slack messages using keywords
- **Ask questions** in plain English and get AI-generated answers with citations
- Works across **multiple channels** at once

---

## Getting Started

### Step 1 — Open the App

Go to: **https://d2586s68tp0zxo.cloudfront.net/**

---

### Step 2 — Connect Your Slack Workspace

1. Click the **"Connect Slack"** button
2. A Slack login popup will appear — sign in and click **Allow**
3. Your workspace will appear in the dropdown automatically

> You can connect multiple Slack workspaces and switch between them.

---

### Step 3 — Load Your Channels

1. Click **"Load Channels"** to see all channels in your workspace
2. Pick the channel you want to work with from the dropdown

---

### Step 4 — Backfill Messages (First-Time Setup)

Before you can search or ask questions, you need to import your Slack messages:

- **"Join + Backfill"** — Join a specific channel and import its messages
- **"Backfill Public"** — Import all public channels at once
- **"Backfill Private"** — Import all private channels the bot is a member of

> You only need to do this once per channel. After backfilling, messages are stored and ready to search.

---

### Step 5 — Search Messages

In the **Search Messages** section:

1. Select the channel(s) you want to search
2. Type a keyword (e.g. `deployment`, `budget`, `launch`)
3. Optionally filter by **date range** or **username**
4. Click **"Search"**

Results show matching messages with the sender's name and timestamp.

---

### Step 6 — Ask the AI

In the **Ask AI** section:

1. Select the channel(s) you want to query
2. Type a question in plain English, for example:
   - *"What did the team decide about the release date?"*
   - *"What did @alice say about the API?"*
   - *"Any action items from last week?"*
3. Optionally set a **date range** to narrow the context
4. Click **"Ask"**

The AI reads your Slack messages and gives a direct answer with **message citations** (e.g. `[1]`, `[2]`) so you can trace every claim back to the original message.

---

## Tips

- Use **`@username`** in your question to filter answers to a specific person (e.g. *"What did @john say about the budget?"*)
- Use **Multi-Channel** mode to search or ask across several channels at the same time
- If you add new channels later, just backfill them to make them searchable
- Click **"Load from DB"** to browse raw stored messages without AI

---

## FAQ

**Do I need a Slack admin account?**
No. You just need permission to install apps in your workspace. If your workspace requires admin approval, ask your Slack admin to approve the app.

**Is my data safe?**
Your messages are stored securely in a private database linked to your session. Only you can access the workspaces you connected.

**The AI gave a wrong answer — what happened?**
The AI only uses messages stored in the database. If a message wasn't backfilled, the AI won't know about it. Try backfilling the channel again and re-asking.

**My session expired — what do I do?**
Sessions last 72 hours. Just reconnect your Slack workspace and you're good to go.

**Can I disconnect my workspace?**
Yes. Click **"Disconnect"** next to your workspace name to remove it and revoke the bot's access.

---

## Support

If you run into any issues, try:
1. Refreshing the page
2. Disconnecting and reconnecting your workspace
3. Backfilling the channel again
