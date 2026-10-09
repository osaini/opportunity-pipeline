# Cold outreach

The Outreach tab from research to reply, call prep, company locations, and the scheduled deep search. Sending and reading mail through Gmail is in [gmail.md](gmail.md).

## Cold outreach pipeline

The Outreach tab runs cold email from research to reply. Every email you send
starts from a draft you approved: it opens in your own email account, where you
press Send, or, with Gmail connected, goes out when you press **Send** in the app
and then confirm the recipient. Three opt-in automations send that approved text
later without another click from you: scheduled sends, the resend after a bounce,
and contact-form submission (your approved first email, once per company; a field
the app cannot answer truthfully, or a picture CAPTCHA, leaves it for you). For a
form left for you, **Finish in browser** on the company's card opens it in a window,
filled in as far as the app can: you fill in the boxes it outlines in orange, solve
any CAPTCHA, and press the form's own send button. The app never presses it there.
It records what the page says once you press (sent, or sent without a
confirmation), and nothing is sent if you close the window first or leave it for 10
minutes. A form
box that requires a street, city, state, ZIP or country is answered only from the
**Mailing address** you confirmed on the Profile page (About you), never any other
address. When a form requires your street address, the rest of your confirmed
address goes into its other address boxes; a form that requires only a country,
state, city or ZIP gets only that, and a form that requires none gets none. A form
that asks for an address twice (a second block for a reference or an emergency
contact) gets no address at all and waits for you, since the app cannot tell
which block is yours.
With no address saved, such a form waits for you and says so. A street box with
no city, state or ZIP box beside it waits for you too: the app does not guess
how that form wants the whole address written on one line. A box that asks for
a home or permanent address, a nationality or a country of birth is never
answered from the mailing address. One
writes its own: the short thank-you after a plain decline is the only email the
app composes and sends without your approval, and only when the keyword rules and
Jev both read the reply as a decline and it passes the sender checks (R1–R7 in
`opportunity_app/outreach/thank_you.py`). Each is a switch under Outreach settings
→ Automation, and pausing automation stops them all. The Outreach page names the
switches that are on, and each draft names the ones that apply to it, including
when automation is paused. [Gmail](gmail.md) says what scheduled sends and the
bounce resend check first.

1. **Find companies.** The deep search runs on Monday and Thursday mornings (or
   **Run deep search now**). Claude Code searches for accelerator startups
   near your target regions, US startups in your field, and recently funded
   companies,
   using only web search and fetch, outside the project directory. Each kind of
   company is its own search of up to 10 companies, run one after another, so
   the broadest one does not crowd out the local search; a later search is told
   what an earlier one found, and one that fails does not lose the others (its
   error shows on the Deep search panel). Python checks
   every proposal before importing it: the website and at least one source URL
   must load. Rejected companies are listed with the reason. Each run writes
   `data/outreach-discovered-<date>-<UTC time>-<run id>.json`.
   A company is never proposed twice. Names are compared without case,
   punctuation, or legal forms ("Acme Robotics, Inc." is "Acme Robotics"), and
   websites by domain. Companies you deleted from the Outreach tab, companies
   you have an application with, and companies with an open posting in your
   feed are rejected with that reason. Recent rejections go back into the next
   prompt, so a company that failed a check is only proposed again with a fix.
2. **Find contacts.** **Find contacts** reads a few pages of the company's own
   site, honoring robots.txt. Addresses published there are *confirmed*.
   Addresses guessed from a named person (following the site's own pattern when
   one is visible) are *unverified*, and are only made when the domain accepts
   mail. Every candidate links its evidence page. The same pages record where
   the company is based when they say so (see **Company locations** below).
3. **Draft.** **Generate draft** writes from your *confirmed* profile facts and
   the target's research only. The model must cite a basis for each claim; a
   draft citing anything else, or stating a number found in neither source, is
   retried once and then refused. Without a model, a template draft uses only
   confirmed facts.
4. **Approve and send yourself.** **Approve draft** refuses unfilled
   `[placeholders]` or a missing recipient, and asks you to accept any other
   warnings (dashes, length). Approval unlocks **Open in Gmail**, a prefilled
   compose window in your account. Editing the draft or changing the recipient
   withdraws the approval. Click **I sent it** after sending; that sets a
   seven-day follow-up.
5. **Follow up and log replies.** Due follow-ups get an in-app reminder (from
   the daily run and the worker) and a **Generate follow-up** draft with the same
   approval step. The follow-up has its own **Follow-up** tab on the card, and
   picking **Follow-ups due** in the rail opens each company there. A due
   follow-up is listed under Follow-ups due, not Drafts to review. With Gmail
   connected, **Approve and send** approves the follow-up and sends it from your
   Gmail in one step; like **Send**, the first click asks you to confirm the
   recipient and the second sends. With Send on their weekday morning on, the
   button is **Approve and schedule for their morning** instead, which queues
   the follow-up for the recipient's next weekday morning (cancellable until it
   goes, and read by the follow-up reviewer first when that is on), with
   **Approve and send now** beside it. Under Follow-ups due each company has a
   box to tick, with **Select all** (Shift ticks a run), and the bar above the
   list queues the ticked follow-ups for their mornings or sends them now. It
   asks once more before anything goes, approves any not yet approved, asks in
   one question about any whose approval raises warnings, and lists every
   company left out and why. Paste a reply into **Log a reply** to get a suggested status
   (call, declined, come back later); nothing changes until you click it. After
   a follow-up goes unanswered for 14 days, the card suggests No response.

**Not interested.** A company you no longer want to pursue gets **Not
interested** on its card. It moves to the Not interested tab and leaves every
other tab, All companies included. It is kept, never deleted: the deep search
and imports still see it as tracked, so it is not proposed again, and **Remove
company** is hidden until you move it back. Nothing automatic acts on it: no
drafts, sends, follow-ups, thank-yous, reminders, Urgent entries, contact
searches, or research, and an email already scheduled for it is cancelled.
Replies from it are still recorded on its card. **Move back to outreach**
returns it to the tab its status puts it in.

**Applied directly.** A company whose own application form you filled in
yourself, with no email or contact form from here, gets **Applied directly** on
its card. It moves to the Applied directly tab and leaves every working tab
(To contact, Ready to send, and the rest), but All companies still lists it.
It is kept and left alone exactly as under Not interested: nothing automatic
writes to it, and Remove company stays hidden until you move it back. The card
shows the day you marked it, not the day you applied.

Settings in `.env`:

```text
PIPELINE_OUTREACH_COMPOSE=gmail            # gmail or mailto (default mailto)
PIPELINE_OUTREACH_ACCOUNT=you@school.edu   # the Google account compose opens in
PIPELINE_OUTREACH_PROVIDER=claude-code     # optional first-email writer; default is the first model set up
PIPELINE_OUTREACH_FOLLOW_UP_PROVIDER=      # optional follow-up writer; empty = same as first emails
PIPELINE_OUTREACH_CALL_PREP_PROVIDER=      # optional call prep writer; empty = same as first emails
PIPELINE_OUTREACH_THANK_YOU_PROVIDER=      # optional writer of the thank-you after a decline; empty = same as first emails
PIPELINE_OUTREACH_REVIEW_PROVIDER=         # optional reviewer of follow-ups and thank-yous; empty = automatic
PIPELINE_OUTREACH_DISCOVERY_PROVIDER=claude-code  # or codex-cli: deep search, locating, Find people (codex-cli needs the next line)
PIPELINE_OUTREACH_RESEARCH_ALLOW_CODEX=    # 1 lets Codex read the web for those and for company research; blank = Claude Code does them
PIPELINE_OUTREACH_COMPANY_RESEARCH_PROVIDER=      # optional company research agent; empty = same as above
PIPELINE_OUTREACH_ATTACHMENT=data/outreach-attachments/resume.pdf  # attached to Gmail drafts
PIPELINE_SEC_USER_AGENT="Your Name you@example.com"  # enables SEC Form D lookups
```

Every AI feature has its own choice under Outreach → Settings, and each list
shows what this computer can run (a provider not set up says so, with how to
set it up): first-email drafts, follow-ups, call prep, the follow-up reviewer,
the web research, company research, and reply and email suggestions (the
keyword rules or Jev).
The Agent and Preparation pages pick per thread and per document from the same
list, subscriptions included. With only one model set up, everything uses it,
and the reviewer says it is from the same company as the writer.

### Company research and call prep

When a company replies, call prep researches it on the web first: its product
and spec pages, customers, what it says sets it apart, competitors, job posts,
patents, papers, grants, GitHub, and news, plus the research agent's own list
of what the web does not say (worth asking on the call). The agent cites a page
and the words on it for every fact. The app opens each page and keeps the fact
only when those words are on it, word for word, with every number, unit, and
name in the fact in them or the lines around them; then a separate read of the
page's own passage (a fresh call that does not see the agent's reasoning; it may
be the same model as the one that wrote the fact) takes the fact beside a passage
the app cut from the page and confirms it says exactly that (the same company or person, the same numbers on
the same things, the same "not"). A kept fact means the page says it, not that
the page is right. A fact that could not be confirmed (a company site that
turns automated readers away, or no second read) is kept marked *not checked*
and never used for questions; one that fails is listed under *Left out* with
the reason.

Call prep also finds who you are talking to: whoever sent the calendar
invitation, else whoever wrote last, from your outreach inbox. With a LinkedIn
test account set under Outreach → Settings (SETUP.md), it reads their profile
for notes on their path, each checked against the profile the same way.

The notes are written to be copied out by hand, with short lines that keep
every specific:

- **ASK**, in call order: questions that get the interviewer talking about
  themselves (rapport, their path, then the product), each opening with
  something you read, then your standing questions in your own words
  (`config/call_prep.local.json`, SETUP.md), each with a research hook when
  there is one and what to have ready;
- **TALKING POINTS** from the email you sent, each with where it lands for them;
- **KNOW**: who you are talking to, a reading of the company marked as a
  reading, and the research, each fact numbered to its source;
- **DURING THE CALL**: blanks for the answers, and a short source list.

Every line the model writes names what it builds on, and a second read leaves
out any line that states more than that. If the call is with someone other
than the person the inbox shows, name them (and paste their LinkedIn profile
link, a linkedin.com/in/ page) under **Talking to someone else?** in the Call
prep tab; what you enter always wins over the inbox.

Research older than 30 days is redone before new notes are written, at most
once a day, and not for a reply the inbox read as a decline. Press **Research
this company** under a company's Research tab to research any company, or run
it for many at once. It needs Claude Code or Codex CLI installed and signed in
here; without one the button says so instead of starting a job that cannot
run. Changing a company's name or website clears its research and interviewer
notes (the history records it), because they were checked against the old
company's pages; call prep researches again:

```bash
python -m opportunity_app.outreach_cli research          # replied companies without recent research
python -m opportunity_app.outreach_cli research --all    # every tracked company, a few minutes each
```

### Company locations and SEC Form D

Each target's location shows where it came from, with a link: your own entry,
the company's site, an SEC Form D filing, or the deep search. A location only
the deep search reported is marked *not yet checked*, and a draft does not say
you are nearby until the site or a filing agrees or you confirm the research.
Your own entry is never overwritten; the company's site outranks a filing.

A draft written before its company's location was checked is not written
again when the location arrives. The app puts its own "(live in ...)" line
right after your school's name in the opening and changes nothing else, and
takes that line out again if the company turns out not to be where you live.
Typing or confirming a location does this at once; with **Write drafts
automatically** on, a location found by the company's site, a filing, or a web
search does it within a minute. An approved draft is left as it is: press
**Add the location line** under it, then approve it again. A draft whose
opening does not name your school as your profile has it keeps its warning,
and you add the line yourself or regenerate it.

- **Company site.** Structured data with a postal address, a sentence like
  "headquartered in Austin, TX", or a street address with a ZIP code. A site
  that lists several places at the same level sets none. Failing those, a site
  that names exactly one place (a footer that just says "Austin, TX") gives it
  as *the only place it names, not yet checked*: the site never says the
  company is based there, so a draft does not rely on it until you click
  **Confirm location**. A site whose pages are empty without JavaScript is
  read again in headless Chromium when Playwright is installed
  (`pip install -r requirements-optional.txt`, then
  `python -m playwright install chromium`). That browser may only load public
  addresses, never this machine or your network, and skips images and styles.
- **Needs a location** in the Outreach rail lists companies you have not
  contacted whose location is missing or not yet checked. Typing a location
  under Research, or **Confirm location** on one shown, settles it.
- **SEC Form D.** A US startup files one after selling shares in a private
  round. EDGAR full-text search finds filings by an issuer with exactly the
  target's name, and the Outreach tab shows the amount sold, the filing date,
  and a link to the filing. Two issuers with the name are reported as
  ambiguous. A filing from a different place than the target's location is
  shown as a possible match and changes nothing, and a filing older than five
  years does not set a location. SEC requires automated clients to identify
  themselves, so set `PIPELINE_SEC_USER_AGENT` to your name and email.

New deep search companies are checked as they are added. To fill in companies
already on the list:

```powershell
py -3 -m opportunity_app.outreach_cli enrich            # targets with no sourced location or Form D yet
py -3 -m opportunity_app.outreach_cli enrich --limit 10 --no-sec
```

It prints how many targets lacked a location before and after. A target is
rechecked at most every 30 days (`--force` overrides that), and the scheduled
deep search runs `enrich --limit 15` after each search.

- **A web search**, for the companies neither their own site nor a filing
  placed. A young startup often states its city nowhere on its site and has
  filed nothing, while one search finds it on its accelerator's page or in a
  funding story. The same headless CLI the deep search uses does the searching,
  and nothing it says is taken on its word: the app opens the page the search
  cites and keeps the location only when that page loads, names the company,
  and states the place. The result is shown as *from a web search* with a link
  to that page, and it ranks below the company's own site and a filing, so
  either one later overrides it. A location you typed is never touched.

```powershell
py -3 -m opportunity_app.outreach_cli locate             # every company nothing else placed
py -3 -m opportunity_app.outreach_cli locate --limit 10 --batch 4
```

A deep search does this for its own new companies; `discover --no-locate`
skips it. Each run reports what it refused and why, so a company with no
sourced location stays visibly empty rather than getting a guess.


### Running the deep search on a schedule

Register the twice-weekly deep search (Claude Code or Codex CLI must be signed
in; run `claude` once):

```bash
python -m opportunity_app.launch install-outreach   # any system
```

On Windows that registers the task below, `.\scripts\install-outreach-task.ps1`.

It runs through `scripts/run-outreach-discovery.vbs`, so no console window
appears. On Windows the task also starts when you sign in, unlock, or wake the
computer, and each of those starts asks whether the newest Monday or Thursday
7:00 already past has a successful run (yours from the Outreach tab counts). If
it has, the start ends at once and writes nothing; if not, the missed search
runs then. A search that failed, or never finished, is tried again no sooner
than three hours later. A task installed before this names no slots, and
there a scheduled run within 48 hours of a successful one is skipped; run
`.\scripts\install-outreach-task.ps1` again to get the catch-up. The log is
`data/outreach-discovery.log`. To try it without changing anything:

```powershell
.\scripts\run-outreach-discovery.ps1 -DryRun
```

Back to the [README](../../README.md) index.
