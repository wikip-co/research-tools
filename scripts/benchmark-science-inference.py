"""Closed-book convenience benchmark using a fixed stratified MMLU-Pro test sample."""

import argparse
import json
import pathlib
import time
import urllib.request
import string
import hashlib

p = argparse.ArgumentParser(
    description="Evaluate a fixed MMLU-Pro science sample using direct JSON answers. This is not the official benchmark protocol."
)
p.add_argument("--label", required=True)
p.add_argument("--base-url", default="http://127.0.0.1:8081/v1")
p.add_argument("--model", default="benchmark")
p.add_argument(
    "--questions",
    type=pathlib.Path,
    required=True,
    help="JSON list of MMLU-Pro rows, frozen before model testing; cot_content is never sent.",
)
p.add_argument("--output", type=pathlib.Path, required=True)
p.add_argument("--thinking", action="store_true")
a = p.parse_args()
rows = json.loads(a.questions.read_text())
questions_sha256 = hashlib.sha256(a.questions.read_bytes()).hexdigest()
a.output.parent.mkdir(parents=True, exist_ok=True)
for row in rows:
    content = (
        row["question"]
        + "\n\n"
        + "\n".join(
            string.ascii_uppercase[i] + ". " + text
            for i, text in enumerate(row["options"])
        )
        + "\n\nReturn exactly one JSON object with an answer field containing the letter of the single best option."
    )
    payload = {
        "model": a.model,
        "messages": [
            {
                "role": "system",
                "content": "Answer the science multiple-choice question. Return only the requested JSON object.",
            },
            {"role": "user", "content": content},
        ],
        "temperature": 0,
        "max_tokens": 2048 if a.thinking else 32,
        "stream": False,
        "cache_prompt": False,
        "chat_template_kwargs": {"enable_thinking": a.thinking},
    }
    if not a.thinking:
        payload["response_format"] = {"type": "json_object"}
    started = time.monotonic()
    record = {
        "label": a.label,
        "question_id": row["question_id"],
        "category": row["category"],
        "expected": row["answer"],
        "mode": "thinking" if a.thinking else "direct",
        "questions_sha256": questions_sha256,
    }
    try:
        req = urllib.request.Request(
            a.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        response = json.load(urllib.request.urlopen(req, timeout=300))
        choice = response["choices"][0]
        msg = choice["message"]
        answer = (
            json.loads(msg.get("content") or "{}").get("answer", "").strip().upper()
        )
        record.update(
            answer=answer,
            correct=answer == row["answer"],
            usage=response.get("usage"),
            timings=response.get("timings"),
            finish_reason=choice["finish_reason"],
        )
    except Exception as e:
        record.update(correct=False, error=str(e))
    record["seconds"] = time.monotonic() - started
    with open(a.output, "a") as f:
        f.write(json.dumps(record) + "\n")
    print(
        json.dumps({k: v for k, v in record.items() if k not in ["usage", "timings"]}),
        flush=True,
    )
