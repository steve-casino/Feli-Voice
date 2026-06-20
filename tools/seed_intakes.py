"""Seed the intake spool with fake records for dashboard development.

Run from the repo root:
    .venv/bin/python tools/seed_intakes.py            # adds 4 fake intakes
    .venv/bin/python tools/seed_intakes.py --reset    # nukes the spool first
    .venv/bin/python tools/seed_intakes.py --count 10 # adds N records

The fake records mirror what app/services/intake_spool.py writes for real
calls, so the dashboard renders them the same way. Safe to run multiple
times — each run appends fresh records.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.config import settings  # noqa: E402

_FIXTURES = [
    {
        "from": "+14045551001",
        "language": "en",
        "summary": (
            "Name: Maria Garcia\n"
            "Callback: +14045551001\n"
            "Language: English\n"
            "Matter: personal injury\n"
            "Reason: Car accident on I-285 last night; rear-ended at a stoplight.\n"
            "Incident date: 2026-06-13\n"
            "Urgency: urgent\n"
            "Notes: Reports neck pain; went to the ER but no admission."
        ),
        "transcript": [
            {"role": "assistant", "content": "Thank you. Our office is closed right now, but I can take your information and have an attorney call you back the next business day. May I have your name, please?"},
            {"role": "user", "content": "Maria Garcia."},
            {"role": "assistant", "content": "Thank you, Maria. What is the best number for the attorney to reach you?"},
            {"role": "user", "content": "Four oh four five five five one zero zero one."},
            {"role": "assistant", "content": "I have four oh four, five five five, one zero zero one. Is that right?"},
            {"role": "user", "content": "Yes that's right."},
            {"role": "assistant", "content": "Thank you. What happened?"},
            {"role": "user", "content": "I got rear-ended last night on 285. My neck is killing me, I went to the ER but they let me go."},
            {"role": "assistant", "content": "I'm sorry that happened. Is the matter urgent for you?"},
            {"role": "user", "content": "Yes I want to talk to someone tomorrow if I can."},
        ],
    },
    {
        "from": "+14045552002",
        "language": "es",
        "summary": (
            "Name: Juan Pérez\n"
            "Callback: +14045552002\n"
            "Language: Spanish\n"
            "Matter: family law\n"
            "Reason: Wants to start a divorce; two kids involved.\n"
            "Incident date: unknown\n"
            "Urgency: routine\n"
            "Notes: Caller is in Atlanta; spouse is in another state."
        ),
        "transcript": [
            {"role": "assistant", "content": "Gracias. Nuestra oficina está cerrada en este momento, pero puedo tomar sus datos para que un abogado le devuelva la llamada el siguiente día hábil. ¿Me puede decir su nombre, por favor?"},
            {"role": "user", "content": "Juan Pérez."},
            {"role": "assistant", "content": "Gracias, Juan. ¿Cuál es el mejor número para que el abogado le devuelva la llamada?"},
            {"role": "user", "content": "Cuatro cero cuatro, cinco cinco cinco, dos cero cero dos."},
            {"role": "assistant", "content": "¿Por qué nos llama hoy?"},
            {"role": "user", "content": "Quiero empezar el divorcio. Tenemos dos niños y mi esposa está en otro estado."},
        ],
    },
    {
        "from": "+14045553003",
        "language": "en",
        "summary": (
            "Name: unknown\n"
            "Callback: +14045553003\n"
            "Language: English\n"
            "Matter: estate planning\n"
            "Reason: Father passed away last week, wants to know about probate.\n"
            "Incident date: 2026-06-08\n"
            "Urgency: routine\n"
            "Notes: Caller did not give name before ending the call."
        ),
        "transcript": [
            {"role": "assistant", "content": "Thank you. Our office is closed right now, but I can take your information and have an attorney call you back the next business day. May I have your name, please?"},
            {"role": "user", "content": "Um, can I just leave a number? It's about my dad's estate."},
            {"role": "assistant", "content": "Of course. What is the best number to reach you?"},
            {"role": "user", "content": "Four oh four five five five three oh oh three."},
            {"role": "assistant", "content": "Thank you. What happened?"},
            {"role": "user", "content": "My father passed away last week and I don't know what to do about his house and the bank accounts. I'll explain more when someone calls me back."},
        ],
    },
    {
        "from": "+14045554004",
        "language": "en",
        "summary": (
            "Name: Aaron Bell\n"
            "Callback: +14045554004\n"
            "Language: English\n"
            "Matter: other\n"
            "Reason: Wants to know if the firm handles immigration; it does not.\n"
            "Incident date: unknown\n"
            "Urgency: routine\n"
            "Notes: Caller seemed satisfied with the referral suggestion."
        ),
        "transcript": [
            {"role": "assistant", "content": "Thank you. Our office is closed right now, but I can take your information and have an attorney call you back the next business day. May I have your name, please?"},
            {"role": "user", "content": "Aaron Bell."},
            {"role": "assistant", "content": "Thank you, Aaron. What is the best callback number?"},
            {"role": "user", "content": "Four oh four five five five four oh oh four."},
            {"role": "assistant", "content": "What is this call about?"},
            {"role": "user", "content": "I need an immigration lawyer for my wife's case."},
            {"role": "assistant", "content": "Thank you for telling me. The firm handles personal injury, family law, and estate planning, so immigration is outside what they take on. I will still pass the message along so an attorney can call you back and may be able to refer you. Is there anything else you'd like me to add to the message?"},
            {"role": "user", "content": "No that's fine, thank you."},
        ],
    },
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed fake intake records for dashboard dev")
    parser.add_argument("--reset", action="store_true", help="Truncate intakes.jsonl first")
    parser.add_argument("--count", type=int, default=len(_FIXTURES), help="How many records to add")
    args = parser.parse_args()

    path = Path(os.path.expanduser(settings.intake_spool_path)).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if args.reset and path.exists():
        path.unlink()
        print(f"Reset {path}")

    now = datetime.now(timezone.utc)
    with path.open("a", encoding="utf-8") as f:
        for i in range(args.count):
            fx = _FIXTURES[i % len(_FIXTURES)]
            # Stagger timestamps so the dashboard shows realistic ordering.
            ended = now - timedelta(minutes=random.randint(5, 720) + i * 17)
            started = ended - timedelta(minutes=random.randint(1, 4))
            rec = {
                "call_control_id": f"seed-{uuid.uuid4()}",
                "from": fx["from"],
                "to": "+14047774002",
                "language": fx["language"],
                "started_at": started.isoformat(),
                "ended_at": ended.isoformat(),
                "summary": fx["summary"],
                "transcript": fx["transcript"],
            }
            f.write(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")
    print(f"Wrote {args.count} fake intake records to {path}")


if __name__ == "__main__":
    main()
