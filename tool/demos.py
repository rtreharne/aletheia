"""The "try it yourself" examples on the landing page.

Each demo is a real assignment (on a demo platform, with no LMS) whose objectives and questions
were written by Claude once, in advance (`python manage.py prepare_demos`), and saved beside the
coursework in tool/demo/<slug>/questions.json. Visitors get them instantly and nothing is generated
per visit. Everything else is the real thing: confirmation, timer, scoring, feedback and the
instructor's view.
"""

import json
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent / "demo"
DEMO_ISSUER = "https://aletheia.demo"
DEMO_USER_PREFIX = "demo-"

DEMOS = [
    {
        "slug": "history",
        "discipline": "History",
        "title": "Why did Peel repeal the Corn Laws?",
        "kind": "Essay",
        "file": "peel-corn-laws-essay.docx",
        "brief": (
            "Essay 2 (1,500 words). Why did Sir Robert Peel repeal the Corn Laws in 1846? Assess the relative "
            "importance of economic ideas, popular pressure from the Anti-Corn Law League, the Irish famine and "
            "Peel's own convictions.\n\n"
            "Objectives should focus on the student's argument: how they weigh the causes against each other, the "
            "evidence they use for each, and the distinction they draw between the occasion and the cause of repeal."
        ),
        "settings": {
            "objective_count": 5, "questions_per_objective": 1, "retries_per_objective": 1,
            "time_each_question": True, "seconds_per_question": 45,
        },
        "highlight": "The defaults",
    },
    {
        "slug": "chemistry",
        "discipline": "Chemistry",
        "title": "Kinetics of the persulfate–iodide clock reaction",
        "kind": "Lab report",
        "file": "clock-reaction-lab-report.pdf",
        "brief": (
            "Experiment 4 lab report. Determine the rate equation for the reaction between peroxodisulfate and iodide "
            "ions by the clock method, calculate the rate constant at room temperature, and find the activation "
            "energy from measurements at five temperatures.\n\n"
            "Objectives should test understanding of the clock method and its assumptions, how the orders and the "
            "activation energy were derived from the data, and the sources of error."
        ),
        "settings": {
            "objective_count": 4, "questions_per_objective": 1, "retries_per_objective": 1,
            "time_each_question": True, "seconds_per_question": 60,
        },
        "highlight": "Formulae and units, 60 seconds a question",
    },
    {
        "slug": "psychology",
        "discipline": "Psychology",
        "title": "Restricted sleep and working memory",
        "kind": "Research report",
        "file": "sleep-working-memory-report.pdf",
        "brief": (
            "Practical report 2. Design and report a small experiment on a cognitive effect of sleep. Your report "
            "should justify your design, report the analysis appropriately and evaluate the limitations.\n\n"
            "Objectives should focus on the design (within-subjects, counterbalancing, the choice of task), the "
            "statistical analysis and its interpretation, and the limitations the student identifies."
        ),
        "settings": {
            "objective_count": 3, "questions_per_objective": 2, "retries_per_objective": 1,
            "time_each_question": True, "seconds_per_question": 40,
        },
        "highlight": "Two questions per objective",
    },
    {
        "slug": "economics",
        "discipline": "Economics",
        "title": "Minimum unit pricing for alcohol in Scotland",
        "kind": "Policy brief",
        "file": "minimum-unit-pricing-brief.docx",
        "brief": (
            "Policy brief (1,200 words). Has minimum unit pricing for alcohol in Scotland worked? Use economic "
            "theory and the published evidence, and make recommendations.\n\n"
            "Objectives should test the economic reasoning (externalities, price floors versus taxes, elasticity), "
            "the student's reading of the evidence, and their argument about whether the policy is regressive."
        ),
        "settings": {
            "objective_count": 4, "questions_per_objective": 1, "retries_per_objective": 0,
            "time_each_question": False, "seconds_per_question": 45, "time_limit_minutes": 5,
        },
        "highlight": "One 5-minute limit for the whole quiz, no retries",
    },
    {
        "slug": "nursing",
        "discipline": "Nursing",
        "title": "A near-miss medication error",
        "kind": "Reflective account",
        "file": "medication-near-miss-reflection.txt",
        "brief": (
            "Reflective account. Using Gibbs' reflective cycle, reflect on an incident from placement that "
            "affected patient safety. Link your analysis to theory and to the NMC Code, and finish with an action plan.\n\n"
            "Objectives should test the student's understanding of why the incident happened, why it mattered "
            "clinically, and what they will do differently, not recall of the reflective model's stages."
        ),
        "settings": {
            "objective_count": 3, "questions_per_objective": 1, "retries_per_objective": 2,
            "time_each_question": True, "seconds_per_question": 30,
        },
        "highlight": "Up to two retries, 30 seconds a question",
    },
    {
        "slug": "code",
        "discipline": "Computing",
        "title": "Weather station data pipeline",
        "kind": "Code viva · Python project",
        "file": "weather-pipeline.zip",
        "brief": (
            "Assignment 2: Weather station pipeline (Python, individual). Write a program that reads hourly readings "
            "from a CSV file, treats implausible temperatures and isolated spikes as missing, fills gaps of up to "
            "3 hours by linear interpolation, summarises each day with enough readings, and reports heatwaves "
            "(three or more consecutive days at or above 27 °C). Include unit tests. Submit a .zip of your project.\n\n"
            "Objectives should test whether the student can explain and predict the behaviour of their own "
            "cleaning and heatwave code: the order of the cleaning steps, what happens at the edges of the data, "
            "the assumptions behind the interpolation, and what their tests cover."
        ),
        "settings": {
            "objective_count": 4, "questions_per_objective": 1, "retries_per_objective": 1,
            "time_each_question": True, "seconds_per_question": 60, "code_viva": True,
        },
        "highlight": "Code viva: questions quote the student's code",
    },
]

BY_SLUG = {demo["slug"]: demo for demo in DEMOS}


def get(slug):
    return BY_SLUG.get(slug)


def file_path(demo):
    return DEMO_DIR / demo["slug"] / demo["file"]


def questions_path(demo):
    return DEMO_DIR / demo["slug"] / "questions.json"


def prepared(demo):
    """The saved objectives and questions, or None if `prepare_demos` hasn't been run for this demo."""
    path = questions_path(demo)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def settings_summary(demo):
    """Short, readable chips describing the demo's settings."""
    s = demo["settings"]
    chips = [f"{s['objective_count']} objectives"]
    if s.get("questions_per_objective", 1) > 1:
        chips.append(f"{s['questions_per_objective']} questions each")
    retries = s.get("retries_per_objective", 1)
    chips.append("no retries" if retries == 0 else f"{retries} retr{'y' if retries == 1 else 'ies'}")
    if s.get("time_each_question", True):
        chips.append(f"{s['seconds_per_question']} s per question")
    else:
        chips.append(f"{s['time_limit_minutes']} min for the quiz")
    if s.get("code_viva"):
        chips.append("code viva")
    return chips


# ---------------------------------------------------------------- database side

def platform():
    from .models import Platform

    obj, _ = Platform.objects.get_or_create(
        issuer=DEMO_ISSUER,
        client_id="demo",
        defaults={"name": "Aletheia demo", "auth_login_url": DEMO_ISSUER + "/", "jwks_url": DEMO_ISSUER + "/"},
    )
    return obj


def assignment(demo):
    """The demo's assignment, kept in step with the registry above."""
    from .models import Assignment

    fields = {
        "title": demo["title"], "brief": demo["brief"], "context_title": "Aletheia demo", "configured": True,
        "max_attempts": 0, "room_word_required": False, "code_viva": False, "time_limit_minutes": 10,
        "time_each_question": True, "retries_per_objective": 1, "questions_per_objective": 1,
        **demo["settings"],
    }
    obj, created = Assignment.objects.get_or_create(
        platform=platform(), deployment_id="demo", resource_link_id=f"demo-{demo['slug']}", defaults=fields
    )
    if not created and any(getattr(obj, k) != v for k, v in fields.items()):
        for k, v in fields.items():
            setattr(obj, k, v)
        obj.save()
    return obj


def is_demo(assignment_obj):
    return assignment_obj.platform.issuer == DEMO_ISSUER


def slug_for(assignment_obj):
    return assignment_obj.resource_link_id.removeprefix("demo-")
