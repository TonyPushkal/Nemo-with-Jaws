#!/usr/bin/env python3
"""Check the local model (Ollama) on the two Phase 1 tasks and measure it on this machine.

    python scripts/local_model_check.py                      # synthetic résumé + synthetic jobs
    python scripts/local_model_check.py --resume my.txt      # your résumé (text/markdown) instead
    python scripts/local_model_check.py --from-probe probe_out/<timestamp>   # real returned job content

Prerequisites: `OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_CONTEXT_LENGTH=4096 ollama serve`
and `ollama pull qwen3.5:4b`. Requests are strictly sequential. Nothing leaves this machine; the
résumé is sent only to the local server. Reports go to probe_out/local_model/<timestamp>/.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from nemo.linkedin import linkedin_job_id
from nemo.probe import _LOGIN, _SIMILAR
from nemo.providers.base import ProviderError
from nemo.providers.ollama import OllamaLLM
from nemo.tasks import assess_job, extract_profile

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"


def sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
    except Exception:  # noqa: BLE001 - diagnostics only
        return ""


def snapshot() -> dict:
    ps = sh(["ps", "-axo", "rss=,command="])
    rss_kb = sum(int(l.split(None, 1)[0]) for l in ps.splitlines()
                 if "ollama" in l and "ps -axo" not in l and "local_model_check" not in l)
    swap = re.search(r"used = ([\d.]+)M", sh(["sysctl", "-n", "vm.swapusage"]))
    po = re.search(r"Pageouts:\s+(\d+)", sh(["vm_stat"]))
    free = re.search(r"free percentage: (\d+)%", sh(["memory_pressure"]))
    return {"ollama_rss_mb": round(rss_kb / 1024), "swap_used_mb": float(swap.group(1)) if swap else None,
            "pageouts": int(po.group(1)) if po else None, "free_pct": int(free.group(1)) if free else None}


class Sampler:
    """Polls memory in a background thread while a single request runs (observation only)."""

    def __init__(self, every: float = 1.0):
        self.every, self.samples, self._stop = every, [], threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            self.samples.append(snapshot())
            self._stop.wait(self.every)

    def __enter__(self):
        self.start = snapshot()
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join()
        self.end = snapshot()

    def summary(self) -> dict:
        s = self.samples + [self.start, self.end]
        val = lambda k: [x[k] for x in s if x[k] is not None]  # noqa: E731
        return {"peak_ollama_rss_mb": max(val("ollama_rss_mb")), "min_free_pct": min(val("free_pct")),
                "swap_used_mb_max": max(val("swap_used_mb")), "swap_delta_mb": round(self.end["swap_used_mb"] - self.start["swap_used_mb"], 1),
                "pageouts_delta": self.end["pageouts"] - self.start["pageouts"], "n_samples": len(s)}


def unload(llm: OllamaLLM) -> None:
    req = urllib.request.Request(llm.base_url + "/api/generate", method="POST", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"model": llm.model, "keep_alive": 0}).encode())
    urllib.request.urlopen(req, timeout=30).read()
    time.sleep(2)


def job_flags(text: str) -> list[str]:
    flags = []
    if len(text.strip()) < 500:
        flags.append("thin(<500 chars)")
    if _LOGIN.search(text):
        flags.append("login-wall text")
    if _SIMILAR.search(text):
        flags.append("similar-jobs list")
    return flags


def load_jobs(args) -> list[dict]:
    if args.from_probe:
        jobs = []
        for f in sorted((Path(args.from_probe) / "raw").glob("*.json")):
            for r in json.loads(f.read_text()).get("results", []):
                jid = linkedin_job_id(r.get("url", ""))
                text = r.get("raw_content") or r.get("content") or ""
                if jid and text and jid not in {j["id"] for j in jobs}:
                    jobs.append({"id": jid, "expect": None, "text": text, "url": r["url"]})
        return jobs[: args.max_jobs]
    return json.loads((FIX / "synthetic_jobs.json").read_text())["jobs"]


def timed(fn, sampler_every=1.0):
    t = time.perf_counter()
    with Sampler(sampler_every) as smp:
        try:
            out, err = fn(), None
        except ProviderError as exc:
            out, err = None, f"{exc.kind}: {exc}"[:300]
    return out, err, round(time.perf_counter() - t, 2), smp.summary()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="qwen3.5:4b")
    ap.add_argument("--num-ctx", type=int, default=4096)
    ap.add_argument("--num-predict", type=int, default=600)
    ap.add_argument("--resume", help="résumé as .txt/.md (default: the synthetic fixture)")
    ap.add_argument("--from-probe", help="probe_out/<timestamp> folder: assess the real returned job content")
    ap.add_argument("--max-jobs", type=int, default=12)
    ap.add_argument("--no-cold-start", action="store_true", help="skip unloading the model first")
    ap.add_argument("--out-dir", default="probe_out/local_model")
    args = ap.parse_args(argv)

    llm = OllamaLLM(args.model, num_ctx=args.num_ctx, num_predict=args.num_predict)
    try:
        installed = llm.installed_models()
    except Exception as exc:  # noqa: BLE001
        print(f"Cannot reach Ollama at {llm.base_url}: {exc}", file=sys.stderr)
        return 2
    if args.model not in installed:
        print(f"Model {args.model} is not installed (have: {installed}). Run: ollama pull {args.model}", file=sys.stderr)
        return 2

    resume_text = Path(args.resume).read_text() if args.resume else (FIX / "synthetic_resume.txt").read_text()
    jobs = load_jobs(args)
    out_dir = Path(args.out_dir) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    env = {"model": args.model, "num_ctx": args.num_ctx, "num_predict": args.num_predict,
           "ram_gb": round(int(sh(["sysctl", "-n", "hw.memsize"]) or 0) / 2**30, 1),
           "chip": sh(["sysctl", "-n", "machdep.cpu.brand_string"]).strip(), "baseline": snapshot()}
    if not args.no_cold_start:
        unload(llm)
        env["baseline_after_unload"] = snapshot()

    rows: list[dict] = []

    def record(name, kind, out, err, wall, mem, extra=None):
        meta = getattr(out, "meta", None) or (out.meta if out is not None and hasattr(out, "meta") else {})
        rows.append({"name": name, "kind": kind, "error": err, "wall_s": wall, "memory": mem, "meta": meta, **(extra or {})})
        m = meta or {}
        print(f"{name:22} {kind:8} wall {wall:6.1f}s  prompt {m.get('prompt_tokens','-'):>5} tok  "
              f"gen {m.get('completion_tokens','-'):>4} tok @ {m.get('gen_tokens_per_s','-')} tok/s  "
              f"{'ERROR ' + err if err else ''}", flush=True)

    # 1) résumé -> profile, twice (first call is the cold start), then a deliberately oversized résumé
    profile = None
    for i in (1, 2):
        out, err, wall, mem = timed(lambda: extract_profile(llm, resume_text))
        record(f"profile#{i}", "profile", out, err, wall, mem, None if not out else {
            "problems": out.problems, "ungrounded_skills": out.ungrounded_skills,
            "resume_chars": len(resume_text), "resume_truncated": out.resume_truncated, "profile": out.profile})
        profile = profile or (out.profile if out else None)
    out, err, wall, mem = timed(lambda: extract_profile(llm, (resume_text + "\n\n") * 5))
    record("profile-oversized", "profile", out, err, wall, mem, None if not out else {
        "problems": out.problems, "ungrounded_skills": out.ungrounded_skills, "resume_truncated": out.resume_truncated})
    if profile is None:
        print("No profile could be extracted; cannot assess jobs.", file=sys.stderr)
        (out_dir / "report.json").write_text(json.dumps({"env": env, "rows": rows}, indent=2))
        return 1

    # 2) (profile, job) -> match, one job at a time; the first job is repeated to check determinism
    for n, job in enumerate(jobs + jobs[:1]):
        name = job["id"] + ("(repeat)" if n >= len(jobs) else "")
        out, err, wall, mem = timed(lambda: assess_job(llm, profile, job["text"]))
        extra = {"chars": len(job["text"]), "flags": job_flags(job["text"]), "expect": job.get("expect")}
        if out:
            extra.update(outcome=out.outcome, model_outcome=out.model_outcome, note=out.note, explanation=out.explanation,
                         fit_quotes=out.fit_quotes, mismatch_quotes=out.mismatch_quotes, dropped_quotes=out.dropped_quotes,
                         limitations=out.limitations, content_level=out.content_level, job_truncated=out.job_truncated,
                         as_expected=(job.get("expect") is None) or out.outcome in job["expect"])
        record(name, "assess", out, err, wall, mem, extra)

    env["loaded_after"] = llm.loaded_models()
    env["final"] = snapshot()
    (out_dir / "report.json").write_text(json.dumps({"env": env, "rows": rows}, indent=2, ensure_ascii=False))

    ok = [r for r in rows if not r["error"]]
    walls = [r["wall_s"] for r in ok]
    print(f"\n{len(ok)}/{len(rows)} requests ok; wall median {median(walls):.1f}s, max {max(walls):.1f}s; "
          f"report: {out_dir}/report.json")
    return 0 if len(ok) == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
