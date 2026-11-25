# Aletheia

A timed comprehension check that a student takes on their own submitted work, delivered inside any LMS that supports LTI 1.3.

1. The student uploads their work (PDF, DOCX or TXT). When Aletheia is a graded assignment, the upload is also handed in to the LMS gradebook as a normal submission, for marking in SpeedGrader.
2. Claude writes learning objectives (5 by default) from the submission and the instructor's brief. Students can view them at any time with the Objectives button.
3. Objectives are tested one at a time. Each question is written just before it's needed, alternating single-answer (MCQ) and multiple-answer (MAQ). By default an objective gets 1 question; a wrong answer earns 1 more on the same objective before moving on.
4. Each question is timed (45 seconds by default). When time runs out, the current selection is submitted; there is no going back. How long each answer took is recorded for instructors (never shown to students): fast correct answers are good evidence that the work is the student's own.
5. The student sees an alignment score, overall and per objective, then can review their answers: each question marked correct or incorrect, with a short explanation for any they got wrong, and how many times they left the page. Answer times are shown only to instructors.
6. Optionally, for invigilated sessions, a **room word** proves students are in the room: the instructor shows a rotating word (it changes every 20 seconds) on a projector, from the dashboard. Students type it to begin, and again before one or two random objectives with 10 seconds to do so. A wrong or missed spot check is flagged; the quiz carries on.
7. A **code viva** option suits programming work: students upload source files, a notebook or a `.zip` of their project (dependencies, build output and `.git` are skipped; indentation is kept). Objectives focus on understanding the code, questions can show a short snippet from it, and a question with a snippet gets 2 extra seconds per line (up to 30).
8. Instructors see every student's alignment and median answer time, each question with its answer and time taken, and an integrity log (leaving the page, switching windows, copy/cut/paste). Aletheia never sets or clears a grade, and adds nothing to the submission beyond the file.

## Run locally

```bash
cp .env.example .env          # add ANTHROPIC_API_KEY
docker compose up --build
```

Open <http://localhost:8014>. Without an LMS, use the development launches. They answer only requests made directly to `localhost` (never through a tunnel or proxy), and can be turned off with `DEV_LOGIN=false`:

- <http://localhost:8014/dev/launch/instructor/>: set up and review the sample assignment
- <http://localhost:8014/dev/launch/student/>: take it as a student (use a different browser or a private window)

Run the tests:

```bash
docker compose run --rm --no-deps -e DATABASE_URL=sqlite:////tmp/test.db web python manage.py test tool
```

## Connecting Canvas

Aletheia needs a public HTTPS address. In `.env` set `DEBUG=false`, `COOKIES_SECURE=true`, `SECRET_KEY`, `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`, then restart.

Canvas registers Aletheia itself, by [LTI Dynamic Registration](https://canvas.instructure.com/doc/api/file.registration.html):

1. In Canvas: **Admin → Developer Keys → + Developer Key → + LTI Registration**, and enter
   `https://<host>/lti/register/canvas`
2. Aletheia registers itself and shows **Continue**. Canvas then shows the new key for review; turn it **On**.
3. In a course, create an assignment with **Submission type: External Tool**, click **Find** and choose Aletheia. Give it points as usual.

| What Aletheia asks Canvas for | Why |
|---|---|
| Privacy level `public` (names and emails) | Instructors see student names on the dashboard |
| Placement `assignment_selection` | Aletheia is added as a graded assignment |
| Scope `lti-ags/scope/score` | Each upload is handed in as a submission, with the file |

This works for Instructure-hosted and self-hosted Canvas alike. A registration never overwrites an existing platform.

Observers (for example parents) are turned away at launch, and staff status comes only from roles in the course itself (not from teaching elsewhere in the institution).

### Other LMSs, or manual setup

Aletheia's tool keys are created automatically at startup (`lti_keys/`, ignored by git). Register Aletheia as an LTI 1.3 tool with:

| Setting | Value |
|---|---|
| OIDC login / initiation URL | `https://<host>/lti/login/` |
| Redirect / launch URL | `https://<host>/lti/launch/` |
| Public keyset (JWKS) URL | `https://<host>/lti/jwks/` |
| Deep linking URL | `https://<host>/lti/launch/` |

Then record the LMS's details in `/admin/` (**Platforms**) or with `python manage.py add_platform` (see `--help`); include `--access-token-url` for gradebook hand-in. Create an admin login with `python manage.py createsuperuser`.

## Landing page

The home page explains Aletheia, offers six **try it yourself** examples, and has a contact form with optional 15-minute call booking.

- **Demos** (`tool/demos.py`, `tool/demo/<slug>/`): five pieces of coursework (history, chemistry, psychology, economics, nursing) with real, DOI-linked references in five citation styles, plus the code-viva project. Each runs on the real quiz engine with its own settings. Their objectives and questions are written once, in advance: `python manage.py prepare_demos` (calls Claude; `--force <slug>` to redo one). Rebuild the coursework files from `source.txt` with `tool/demo/build_files.py`. Demo attempts are deleted after two days.
- **Contact and calls** (`tool/booking.py`): messages are emailed to `CONTACT_EMAIL`. A visitor can pick a call on a weekday, 10:00–16:00 UK time, up to three weeks ahead (shown in their own time zone). They confirm by email; you both then get a calendar invitation with `MEETING_URL`. Until `EMAIL_HOST` is set, emails are printed to the app log (`docker compose logs web`).

## How it works

| Where | What |
|---|---|
| `tool/lti.py` | OIDC login, id_token validation (signature, issuer, audience, nonce, expiry, deployment), roles, deep linking, tool keys |
| `tool/registration.py` | LTI Dynamic Registration |
| `tool/demos.py`, `tool/booking.py` | Landing-page demos; contact form and call booking |
| `tool/ags.py` | Gradebook hand-in (LTI Assignment and Grade Services, with Canvas's submission extension) |
| `tool/ai.py` | Claude calls: one for the objectives, then one per question, with output validation and option shuffling |
| `tool/utils.py` | Text extraction: PDF, Word, text; for code vivas, source files, notebooks and `.zip` projects |
| `tool/templatetags/aletheia.py` | Safe display of AI text: only `<sup>`, `<sub>` and `` `inline code` `` are rendered |
| `tool/room.py` | Room words: the word list, rotation (an HMAC of a per-assignment secret and the time window) and typo-tolerant checking |
| `tool/services.py` | Objective-by-objective flow, questions generated ahead of need, server-enforced timing, scoring |
| `tool/views.py` | Pages for students and instructors |

Timing is enforced on the server: a question's clock starts when it is first served, reloading does not reset it, and answers arriving more than 3 seconds after the limit score nothing.

When a question is shown, its possible follow-up and the next objective's first question are generated in the background, so whichever is needed is usually ready when the student answers. Unused questions are discarded. The submission is sent with prompt caching, so later calls on the same attempt are cheaper.

An MAQ counts as correct only when every correct option, and no other, is selected. An objective's alignment is the share of its questions answered correctly (wrong then right is 50%); overall alignment is the average across objectives.

Instructors set the number of objectives, questions per objective (correct answers needed to clear it), retries per objective, seconds per question (45 by default) and attempts allowed.

The model and reasoning effort are set with `ALETHEIA_AI_MODEL` (default `claude-opus-5-5`), `ALETHEIA_AI_EFFORT` for objectives (default `medium`) and `ALETHEIA_AI_QUESTION_EFFORT` for questions (default `low`, for speed).
