# Aletheia: working notes for Claude

Aletheia (AletheiaIntegrity.com) is an LTI 1.3 tool: a student uploads their work, Claude writes learning objectives from it, then a timed MCQ/MAQ check tests each objective. See README.md for setup, architecture and how to run tests. This file records what the owner has asked for and decided, so new features can be checked against it.

## Ground rules

- **Absolute minimalism.** Ask before adding anything outside the scope below.
- **Completely distinct from MachinaViva / VivaNoodle** (this repo began as a clone). No mention of those names, and nothing visual from them.
- Minimal white theme, Baskerville. Plain monospace for code, no syntax colouring.
- British English everywhere.
- Work on branch `aletheia-rebuild`. Keep a fresh, small migration history.
- Stack: Django + Postgres; local run `docker compose up --build` on port 8014.

## Scope

- **LTI 1.3 only.** No standalone app, blog or load tests. The landing page with demos and contact form is the only public, non-LTI part.
- Keep: minimal instructor dashboard, integrity event logging, attempt limits, Django admin.
- No chat viva.

### Flow

1. **Two-step hand-in.** Upload creates a *draft* attempt, then a confirm page with a preview (PDF inline, otherwise extracted text) and required ticks: "correct file" and "agree to generative AI policy". Only then: hand-in to the LMS and generation starts. Drafts never count as attempts and are hidden from instructors. The AI policy text is set per assignment (sensible default; blank = no tick); the agreed text and times are stored on the attempt and shown to instructors.
2. **Objectives** are generated first from the submission plus the instructor's brief (default 5). Students can view them via the Objectives overlay button.
3. **Questions** are generated one objective at a time, just ahead of need (not all at once). Default 1 question per objective; a wrong answer earns another on the same objective (retries per objective, default 1).
4. **Question style:** address the student as author ("your essay"), parallel options, no trailing full stops. Each question has an AI explanation stored on `Question.explanation`.
5. **AI text rendering:** only `<sup>`/`<sub>` and backtick code pass through the `scripts` filter.

### Leaving the page

- Students are told once, on the Ready page, that leaving the page or switching windows is recorded and shown to the instructor.
- No warnings or pop-ups when they leave (timer pressure, false positives from notifications and assistive tools). Instead a quiet muted line at the top of the next question: "You left the page during the last question. This has been recorded." It never blocks or pauses the timer.
- Screenshots on leaving were discussed and not built: a page can't see outside its tab; screen-share capture is desktop-only, fragile and a privacy problem.

### Timing

- Per-question timer with a shrinking bar, fixed 45 s by default. On expiry, submit what's selected (nothing = wrong), auto-advance, no going back.
- A progressive (shortening) timer was tried and **dropped** (speed confound, accessibility). Instead answer times are recorded and shown **to instructors only** (median per attempt + per question).
- "Time each question" can be turned off; then one whole-quiz limit applies (default 10 min, `time_limit_minutes`). When it runs out the current answer is submitted and unreached objectives score 0.

### Scoring

- Called **"Alignment"** in all user-facing text (code fields still say `mastery`).
- Objective alignment = share correct; overall = mean over **all** objectives. An MAQ counts only if exactly right.

### After the quiz

- Students see alignment, then a feedback page: each question marked correct/incorrect, correct options shown, explanation only for wrong answers, and how many times they left the page (hidden + blur events). Answer times stay instructor-only. Instructors see all explanations.

### Instructor review (also shown in demos)

- Headline figures at the top: Alignment, Flags (red when > 0, with a breakdown: left the page / copy, cut or paste / room word missed), Median answer time. Flagged rows are red in the integrity log.
- "Invite to try again" (`Invitation` model): one extra attempt beyond the limit; pending until the student hands in a new attempt; can be withdrawn. The student sees "Your instructor has invited you to try again" on next opening the assignment. Nothing is sent to the student or posted to the LMS. In demos the button starts a fresh demo attempt.

### Instructor settings (shown on first launch)

Brief, objective count, questions per objective, retries per objective, seconds per question (45), time each question on/off, whole-quiz limit, attempt limit, AI policy text, code viva, room word.

### Options

- **Room word invigilation:** projector page shows a rotating word (every 20 s); students type it to begin (hard gate) and in 1–2 random 10 s spot checks between objectives (failure = flag, quiz continues). Words, not digits. Key-holding was rejected (inaccessible, easily bypassed).
- **Code viva:** source / notebook / `.zip` uploads, code-focused objectives, questions may include a ≤12-line snippet (+2 s per line, max +30 s), inline `code`.

## Canvas / LMS

- Each upload appears in Canvas grades as a normal submission, markable in SpeedGrader: AGS score + Canvas submission extension (file content_items, PendingManual, preserve_score always). **Never set or clear a grade.**
- **Do not add comments to the submission** after the quiz. Nothing is posted beyond the file.
- Registration: LTI Dynamic Registration at `/lti/register/canvas` (no secret key, no host allow-list; HTTPS only; never overwrites an existing platform). Placement `assignment_selection` only. Moodle via `/lti/register/moodle` (untested).
- **Landing copy must be LMS-agnostic.** Only Canvas shows the file as a submission (via its extension); elsewhere the file stays in Aletheia, so say "where your LMS supports it". Untested risk: Moodle may treat our score-less AGS post as clearing an existing grade (`preserve_score` is Canvas-only); if so, skip the AGS post on non-Canvas platforms.
- **"Don't send parents anything":** observers (Mentor role) are refused at launch; staff status comes only from course membership roles.
- **New tab:** inside the LMS frame, Begin opens the questions in a new tab via a signed one-time handoff link (works with partitioned cookies); the frame shows "open in another tab"; on completion the tab says "Finished, close this tab, go back to your course". Auto-redirect to return_url was rejected (didn't work in Canvas). Frame vs tab detected with `Sec-Fetch-Dest`.
- **Pending reminder:** Safari blocks third-party cookies in the Canvas iframe, so launches can fail with "cookies were blocked". Deferred, not dropped: remind the owner once Canvas launches work in Chrome. Possible fixes: cookie check on `/lti/login/` with an "Open in new tab" fallback, and/or Canvas display_type `new_window` in the registration.

## Canvas admin-token route (stages 1–2 built 2026-11-25; 3–5 to do)

A second way in, alongside LTI 1.3, modelled on bioboost-dev's Canvas sign-in. Decisions:
- For pilots that can't install LTI yet. Set up by us in Django admin; not advertised on the landing page; LTI stays the main route.
- Each check is tied to one Canvas assignment: Aletheia writes its iframe into the assignment description (and sets online upload) through the API. Uploads are handed in on the student's behalf via the API (file upload to `submissions/<user_id>/files`, then `submission[user_id]`), using grading rights, never a grade. Not `as_user_id` (needs "act as users", which Robert's sub-account admin role lacks).
- Tokens stored encrypted in the database, one per institution, from a dedicated "Aletheia" Canvas account (development temporarily uses BioBoost's token on canvas.liverpool.ac.uk, restricted to a sandbox course).
- Sign-in: student searches the class list by name in the iframe, Aletheia sends a one-time link to their Canvas inbox, the link opens a temporary Canvas page whose iframe exchanges a one-time code for a 12-hour session on that device, then returns them to the assignment.
- Staff roles: teacher, TA, designer. Observers refused. Canvas Student View can't sign in (no inbox), and Canvas "Act as user" isn't available to us, so staff get "Preview as student" inside Aletheia: the full student flow as themselves, nothing handed in, hidden from the dashboard, never billed.
- Stage 0 findings (2026-11-25, sandbox course 53523): no admin rights needed, only teacher/grader in the course (roster, messages, assignment edits, submit on behalf all work). Canvas keeps iframes and data-* attributes in descriptions but strips `referrerpolicy`, so the sign-in code must travel in the iframe's own src (temporary Canvas page). Firefox: partitioned cookies and local/session storage survive from the temporary page to the assignment. Chrome and Safari not yet tested; Safari must pass the cookie-free (token in requests) fallback.
- Offboarding removes Aletheia's iframes from assignments and deletes the token; assignments and submissions stay.
- Built (stage 1): `tool/canvas.py` (encrypted token, allow-listed API calls, check, class-list sync, link/unlink, offboard), models `CanvasConnection`/`CanvasCourse`/`CanvasMember` plus `Assignment.canvas_course`/`canvas_assignment_id`, Django admin (Check connection, Sync class list, Link or unlink assignments, End pilot with confirmation), `manage.py canvas_connect` (token on stdin). The iframe page `/canvas/a/<id>/` is a placeholder until stage 2 (sign-in).
- Built (stage 2): sign-in in `tool/canvas_views.py` (name search, rate-limited inbox link valid 1 hour and once, temporary Canvas page with a 10-minute one-time code, then the same session entry an LTI launch makes plus `canvas_until`, 12 hours). Not signed in on a Canvas-linked assignment redirects to the sign-in iframe. "Preview as student" (both routes): staff try the student side as `preview:<their id>`; never handed in, hidden from the dashboard, attempts deleted on Stop preview. All SameSite=None cookies are marked Partitioned (CHIPS) by `tool/middleware.py`, which also affects LTI.
- Dev data: connection "University of Liverpool (development)" allows only course 53523; Canvas assignment 363477 "Aletheia check (test)" (unpublished) is linked to Aletheia assignment 16.

## Landing page

- No tagline (the owner removed "the unconcealed truth of integrity"). Hero = wordmark, rule, "Check that students understand the work they hand in."
- Sections: overview, Why Aletheia, how it works, six "try it yourself" demos, settings showcase (no room word), LTI 1.3 only (Canvas + Moodle), "alongside your grading", contact form + optional 15-min call.
- Hero intro animation ("revealing the truth"): ALETHEIA surfaces from a blur letter by letter before anything else; then the rule draws from the centre and the lede, sublede and buttons rise in (~3.5 s total; none with reduced motion). Hero is full screen height (100svh) with a thin down chevron to #why that fades in last and drifts gently; anchor links scroll smoothly.
- Scroll animations (chosen 2026-11-25, all play once, none with reduced motion): section headings draw a short rule beneath them; the four steps surface from a blur in turn; a sample question card (real history-demo question) answers itself (timer runs, option picked, Next, "Answered in 9.6s · recorded for the instructor") with a Play again link. Avoid parallax, typewriter text, counters and looping motion.
- How it works: four numbered steps, then the sample question card, then a bordered "Optional · In the room" panel (before and during step 3) with a mock projector word that rotates, and a/b/c sub-steps: on the screen, to begin, along the way. The room word stays out of the settings showcase.
- Section dividers span the content width (57.5rem), not the full window.
- No corner navbar (the owner hated it). A sticky top bar appears after the hero (mark, section links, Book a call).
- Demos: history .docx (Harvard), chemistry .pdf (ACS), psychology .pdf (APA 7), economics .docx (Chicago), nursing .txt (Vancouver), code viva .zip. Real Crossref-verified DOIs. Questions are pre-generated into `tool/demo/<slug>/questions.json`, so no per-visit API cost.
- Calls: weekdays 10:00–16:00 UK, email confirmation by POST button, .ics invites. Contact address hello@aletheia.integrityed.com. Console email backend until SMTP is configured; `MEETING_URL` not yet set.

## AI cost (changed 2026-11-25 to bring cost down)

- Objectives: Claude Opus 5.5 at medium effort, one uncached request. Questions: Claude Sonnet 5.5 (`ALETHEIA_AI_QUESTION_MODEL`) at low effort, with the submission cached.
- Retry questions are written only after a wrong answer (student waits a few seconds); the next objective's first question is still prefetched.
- Every Claude response's tokens and cost are added to the attempt (`ai_*` fields, shown in Django admin). `python manage.py measure_costs` runs the demos through the real engine with a simulated student and reports cost (spends real money).

## Pricing (decided 2026-11-25; shown in the landing page's Pricing section, which must stay unambiguous)

- Buyer: departments or schools. GBP only, prices shown excluding VAT ("+ VAT").
- £5 per student per academic year, 1 Sep to 31 Aug (raised from £4 on 2026-11-25 after measuring AI cost at about 5p per viva). A student counts once if they hand in at least one attempt in that year, across all courses and assignments.
- Minimum 100 students (£500 + VAT) per department per academic year, invoiced on order; students above 100 invoiced after 31 August. Invoice, 30 days, purchase orders accepted.
- Unlimited attempts at no extra charge (the 10-attempt cap, and later the per-10-attempts surcharge, were both dropped on 2026-11-25). Billing counts students with at least one hand-in (confirmed attempt), not drafts.
- Included: every feature (no tiers), setup call, email support replying by end of next working day, data processing agreement.
- No auto-renewal: each academic year is a new order.
- Pilot: "Try it for a semester. No obligation." One course, up to 300 students, one semester, free.

## Legal documents (drafted 2026-11-25; need solicitor review before use)

- `/terms` and `/privacy` pages, and a Word DPA template at `tool/static/tool/legal/aletheia-dpa-template.docx` (rebuilt by `legal/build_dpa.js`). Linked from the landing footer and the Pricing section.
- Company, hosting, email/video provider and transfer details come from env settings (`LEGAL` in `lti/settings.py`); unset ones show as highlighted [placeholders]. Seller is a UK limited company; hosting will be a UK or EU cloud provider.
- Roles: institution is controller for student/staff data, we are processor; we are controller for website visitors and enquiries. Anthropic (US) is the AI sub-processor and receives submission text, title and brief only, never names or LMS IDs.
- Retention promised: student data deleted 31 August a year after the academic year ends; demos 2 days; enquiries 24 months (contract records 6 years); sessions 2 weeks; abuse-prevention IPs 1 hour. NOT yet enforced by the app except demos.
- Fonts are self-hosted (no Google Fonts); the site must load nothing from third parties, because the privacy notice says so.

## Environment

- Public dev tunnel: https://aletheia.uniwebdev.co.uk → 127.0.0.1:8014 (cloudflared config `~/.config/cloudflared/aletheia`).
- With `DEBUG=false` static files use a manifest, so CSS/JS edits only show after `docker compose exec web python manage.py collectstatic --noinput` and `docker compose restart web`.
- `/dev/launch` must stay localhost-only (it was briefly exposed publicly under `DEBUG=true`).
