# Privacy and accessibility acceptance

Data is private by default. Profile/resume/application content is used only for the student's workflow. Employer access requires an item-level, named-recipient, expiring consent grant; revocation is checked at read time. Account and dossier exports are machine-readable. Account deletion removes user-owned relational records and private files while retaining only a one-way user hash in the deletion log. Connector secrets are redacted from exports and erased on disconnect.

The web UI uses semantic headings, labels, live status regions, visible focus, reduced-motion rules, buttons for every swipe/drag action, a list alternative to cards, and keyboard stage controls. Recorded mock interviews require a visible explicit control and have a typed path. Automated/static checks and keyboard tests are included; public launch still requires a manual screen-reader pass with NVDA, VoiceOver, and TalkBack on the staging build.

No automated score is a hiring decision. Employer ranking counts only explicit rubric evidence. Protected criteria are rejected. Because the platform does not collect demographic data without consent, it reports subgroup metrics as unavailable; it never substitutes proxies or claims a fairness threshold passed without evidence.
