# Gmail: sending, replies and labels

Connecting Gmail, what the app reads and sends, and the one-time Google Cloud setup (see [Gmail drafts setup](#gmail-drafts-setup)).

## Sending through Gmail, with an attachment

A compose link cannot attach a file. To attach your resume, connect Gmail. An
approved draft then shows two buttons:

- **Send with resume.pdf** sends it from your Gmail without leaving the app.
  The first click only asks: the button becomes **Send to jane@company.com?**,
  and a second click sends. Escape, clicking elsewhere, or waiting eight seconds
  cancels. A successful send marks the company Sent and sets the follow-up a
  week out, the same as **I sent it**. Each email goes out at most once, and
  nothing is sent if the draft changed after you confirmed it.
- **Open in Gmail with resume.pdf** creates the draft in your Gmail Drafts
  folder and opens it, for when you want to edit it there first. Clicking again
  for the same approved words reopens the same draft. Once a draft exists, send
  it from Gmail and press **I sent it**: the app never sends a Gmail draft, since
  it may have been edited there, and it refuses to send its own copy while the
  draft is still in Drafts.

The app sends only the words you approved, and does not send an email a second
time on its own:

- Two clicks, two tabs, or a retry cannot send twice. Only one send or draft of
  an email runs at a time.
- If Gmail does not confirm a send (a timeout or a Google error), the email may
  have gone out. The app then asks you to check your Gmail Sent folder before it
  sends again. The button becomes **Checked Gmail — send again**, and that
  covers one attempt. The app cannot read your Sent folder, so this check is
  yours: if the email is there, press **I sent it** instead.
- If a Gmail draft of the email has left your Drafts, it may have been sent from
  Gmail, so the app asks the same question.
- Once an email was sent, or marked sent by hand, no new Gmail draft of it is
  made.

Gmail accepting a send does not mean it arrived: a company's server can refuse
it seconds later and send back a delivery failure notice. The app looks for
one in the sent email's thread, and searches your inbox for notices a server
sent outside the thread, each time the Outreach list loads and a few times in
the minutes after a send, for three days. The notice's delivery report says
exactly which recipient failed, so a bounced Cc alone leaves the email sent. When it finds one, the
company goes back to **Drafted** under **Bounced**, with no follow-up
scheduled, and the failed address is never sent to again. Pick another
contact: the draft's greeting ("Hi Dana," or "Hi Acme team,") changes to match
without a model call, you approve the draft again, and it can be sent once to
the new address. A notice pasted into **Log a reply** is caught too, and is
never logged as a reply.

Replies are read from Gmail too, so there is nothing to paste, and a reply
from someone other than the address you wrote to is not missed. For each
company you wrote to in the last six months, the app looks at mail from the
addresses you wrote to, from anyone at the company's own domains (its
website's, others its site shows it mailing from, and your contact's when it
shares the website's name), in the Gmail thread of any email you sent them
whoever wrote it, and mail from a person writing to you that names the
company (when its name is specific enough: two words, or one of six letters
or more) or your email's exact subject. Spam is looked in too; Trash is not.

What it finds is sorted by how sure the app can be:

- **A reply** is logged as a pasted one would be, starts call prep, and moves a
  company that was waiting (or marked No response, or back to Drafted after a
  bounce) to **Replied**. That is someone at the company answering in the
  thread of your email, an address you wrote to writing back to you (not a
  blast you were blind-copied on), or a person at the company's own website
  domain writing to you: Gmail verified the sender, you were in the To or Cc
  line, and their name matches their address. The card and the history say
  how each one was matched ("Found in Gmail").
- **A possible reply** is anything weaker a person may have written: a shared
  or team inbox (careers@, recruitment@), a colleague Gmail could not verify,
  someone outside the company writing in your thread or someone you copied,
  mail in Spam or sent through a mailing or sales tool, mail an applicant
  system sent (even in your contact's name), mail that could be from two
  companies you wrote to, mail naming the company from a personal address,
  and anything the app found that arrived before these checks existed. It shows on the company's card with why,
  in **Urgent**, and as a notice. Say **It's a reply, log it** or **Not a
  reply**. Until you do, nothing automatic treats the company as silent: a
  scheduled follow-up waits (it is not cancelled), closing as No response and
  automatic follow-up drafts hold, an automatic resend after a bounce waits
  for you, and a resend you had scheduled is cancelled.
- **Set aside**, with the reason kept: your own mail, delivery notices, mail
  from before your first email, mailing-list mail from a shared or automated
  sender, automated senders at the company that are not answering you (account
  and security mail), and mail in Spam that Gmail could not verify, unless it
  is from a person at the company or an address you wrote to.

What a reply means (declined, a call, an offer) is shown as a suggestion you
apply or dismiss. An out-of-office reply (in any of the common languages) is
noted and changes nothing. For research outreach to a university, the same
person's other address there counts as the one you wrote to, and others in
their department are possible replies at most. The app
checks every few minutes in the background while it runs, and whenever the
Outreach list loads. It still cannot see a reply sent to another mailbox than
the Gmail you connected, or a fresh email from a personal address, outside
your thread, that names neither the company nor your subject.

With **Send on their weekday morning** switched on under Outreach settings →
Automation, the confirmed click schedules the approved email instead of sending
it: it goes out between 9:00 and 9:40 on the recipient's next weekday, in the
timezone of the company's US state (or yours, when the location names none;
the card says which). The app sends it through the same once-only path while
it runs. Editing the draft or changing the recipient cancels it, and the card
keeps **Cancel** and **Send now**. A send that could not go (Gmail unreachable
three times, the draft sent some other way) is shown on the card with why.

While a draft is waiting for review, the card also offers **Confirm research,
approve and schedule** (or **Approve and schedule for their morning** when the
research is already confirmed). After a second press naming the recipient, it
does what **Confirm research**, **Approve draft** and **Schedule for their
morning** do, in that order, with the same checks; approval warnings still ask
first. If a step stops, the earlier ones stay done and the message says where
it stopped. The separate buttons are still there.

The app sends scheduled email only while this computer is awake, and the Gmail
API cannot schedule a send. A scheduled email that missed its morning by more
than two hours (the computer was asleep or off) is never sent late: it moves to
the recipient's next weekday morning, and the card and history say so. To send
at a set time with the computer off, use **Open in Gmail** and Gmail's own
**Schedule send** (the arrow next to Send). The app notices when Google sends
it (the draft leaves Drafts and appears in Sent), marks the company sent with
the real date, and watches for bounces and replies as for any other send. A
draft waiting in Gmail's Scheduled folder is noted in the history.

Just before any scheduled email goes out, the app reads Gmail again for a
bounce or a reply about that company, instead of trusting the last background
check. A follow-up is never sent to a company that replied or whose first email
bounced, and if Gmail cannot be read the email waits. With **Have a second model
check each follow-up** on, a second model reads each follow-up with the whole
thread before it goes. Pick it under **Who reviews follow-ups**; on Automatic it
is a model from a different company than the follow-up writer when one is set
up, and the same one (said plainly) when it is the only model here. It goes only
on a clean pass; an out-of-office with a return date
holds it until then, and anything else stops it with the reviewer's reasons on
the card. If the reviewer cannot run, the follow-up waits rather than going
unchecked.

The app requests three scopes. `gmail.compose` covers drafts and sending.
`gmail.readonly` covers bounces and replies. `gmail.modify` (Google words it as
"Read, compose, and send emails from your Gmail account"; tick it on the consent
screen) is used only to add your outreach label. With readonly the app reads the headers
of its own sent threads, delivery failure notices, mail from the companies you
wrote to (Spam included), mail in the threads of the emails you sent them, and
mail naming those companies or your emails' subjects, and, to label the emails
you sent, the To, Cc, Bcc and Subject headers (never the body) of the emails you
send, and nothing else. It lists recent mail by id to find what is in those
threads, reading only those messages. To label the emails you sent, it searches
your Sent mail for each company (by that company's addresses and the subjects only
that company uses, from 30 days before you added it, and again if you change its
addresses, subjects or dates), then lists the mail you sent since its last check and reads only those headers
of each new sent email, to tell whether it went to a company you wrote to. The app calls only `drafts.create`, `drafts.get`, `messages.send`,
`threads.get` (metadata format), `messages.list` (searches for notices from
`mailer-daemon` or `postmaster`, for mail from or naming those companies, and
recent mail by id and thread), `messages.get` (for what those searches find),
`labels.list`, `labels.create`, `threads.get` (metadata format: the From,
Subject, Content-Type and X-Failed-Recipients headers, to tell delivery failure
notices apart), `messages.list` (`in:sent` searches: per company by its addresses and subjects, then mail sent since the last check), `messages.get`
(minimal format for a reply's thread, metadata format for a sent message's
recipients and subject), `messages.batchModify` (adds the label only), and `profile`.
A connection made before the bounce check or the label existed
still sends; the tab asks you to reconnect once to turn them on.

**Outreach labels.** Every outreach thread gets one Gmail label, `opportunities`
by default: the emails you sent to companies, first emails and follow-ups
included, whether or not anyone replied, and every confirmed reply, in each case
the whole thread including messages that arrive later. Delivery failure notices
and drafts are left out. Emails the app sent are found from its own record;
emails you sent from Gmail are found by searching your Sent mail once for each
company that has gone out (by its addresses and the subjects only it uses, from 30
days before you added it, up to 500 results a search, each hit checked against the
company's exact addresses and subjects; searched again if you change its addresses,
subjects or dates, and a subject two companies share is never used, since it
cannot say which company mail belongs to), and each new sent email's recipients and
subject are checked the same way afterwards. Each
student can rename the label or turn it off (empty) under Outreach → Outreach settings
→ "Gmail label for replies" (letters, digits, spaces, hyphens, underscores and slashes only). Threads that existed before the label are
labelled once in a backfill. Pausing automation pauses labelling. The app never
removes a label, and never deletes, archives, moves, or marks mail read. The
connection is refused if Google signs in as an account other than
`PIPELINE_OUTREACH_ACCOUNT`.

**Reading the pipeline mailbox from an agent.** The pipeline mailbox is the
account the app connected to; a coding agent's own Gmail tool may be another
account. `scripts/pipeline_mailbox.py` (`whoami`, `search "label:opportunities"
[--max N]`, `thread THREAD_ID`) reads the pipeline mailbox, read-only, and can
read any message in it. `whoami` says whether `label:` alone can be trusted:
only when it reports 0 outreach threads not labelled yet, 0 that the app could not label
(Gmail refused them, or left them out of their thread; threads whose mail Gmail no longer has are not counted, since no search finds them)
and 0 companies not yet searched for sent outreach. Its output goes into the agent's conversation and to the
agent's model provider. In Claude Code a hook reminds the agent once per session
before a Gmail tool runs (needs Node).


## Gmail drafts setup

One-time setup:

1. In the [Google Cloud console](https://console.cloud.google.com/), create a
   project, enable the **Gmail API**, and configure the OAuth consent screen.
   Add your sending account as a test user if the app is External.
2. Create an OAuth client of type **Web application** with these authorized
   redirect URIs:
   `http://127.0.0.1:8765/connections/oauth/gmail_drafts/callback` and
   `http://localhost:8765/connections/oauth/gmail_drafts/callback`.
3. In `.env`, set `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
   `PIPELINE_OUTREACH_ACCOUNT` (your address), `PIPELINE_OUTREACH_COMPOSE=gmail`,
   and `PIPELINE_OUTREACH_ATTACHMENT`. `setup init` already generated
   `PIPELINE_CONNECTION_KEY`, the Fernet key the tokens are stored encrypted
   with. Restart the app (`python -m opportunity_app.launch restart`).
4. Click **Connect Gmail** on the Outreach tab.

An External app left in Testing status gets refresh tokens that expire after
seven days; the tab then offers **Reconnect Gmail**. A Google Workspace school
account may also block unverified apps from Gmail scopes.

Back to the [README](../../README.md) index.
