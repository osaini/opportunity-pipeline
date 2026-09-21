# Assisted Apply Chrome extension

This is a clean-room, local-first assisted-apply tool. It never clicks Submit,
Next, consent, CAPTCHA, or messaging controls and never infers that an
application was submitted.

## Install and pair

1. Start the tracker on its loopback address (the default is
   `http://127.0.0.1:8765`).
2. Open `chrome://extensions`, enable Developer mode, choose **Load unpacked**,
   and select this `apps/extension` directory. Chrome 114 or newer is required.
3. In the tracker's **Profile** page, find **Assisted Apply extension** and
   create a one-time pairing code.
4. Click the extension toolbar icon. Enter the loopback server origin and code
   in the persistent side panel.

Codes work once and expire after 10 minutes. The browser stores only a
revocable device credential and value-free retry metadata. Confirmed profile
values, answers, and file bytes remain in memory and are never written to
extension storage. The server stores only the token's SHA-256 digest.

## Use

Open an external ATS application, choose its explicit pipeline match, inspect
the score and source context, then scan the current step. Check only the fields
you reviewed. Repeat the scan after you manually advance to another step.

Approved PDF/DOCX documents require explicit per-application selection. If the
ATS rejects attachment, use the panel's manual download fallback. Tailored
documents cannot be used for a different opportunity.

After personally submitting, check the confirmation box and choose **Mark as
submitted**. Page text or a success-looking URL can never update the tracker.

## Privacy, revocation, and troubleshooting

- The extension has transient `activeTab` access and no blanket job-site host
  permission. Its only optional network permission is `http://127.0.0.1/*`.
- Revoke a paired browser from the Profile page. A revoked panel fails closed
  and cannot use any general student, employer, or admin API.
- If the tracker is offline, restart it and reopen the panel. Only field outcome
  metadata—not proposed values or files—is eligible for retry.
- Cross-origin frames, closed shadow roots, CAPTCHA, and unsupported custom
  widgets stay manual by design.
- LinkedIn Easy Apply is intentionally unsupported pending a separate platform-
  policy and account-state review.

Run the fast field-engine regression suite with:

```bash
node tests/extension/run_tests.mjs
npm run test:extension:browser
```
