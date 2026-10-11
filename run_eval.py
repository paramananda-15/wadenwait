#!/usr/bin/env python3
"""Benchmark: direct-depth prompting (baseline) vs perception + geometry (ours), per model.

  export OPENROUTER_API_KEY=...
  python eval/run_eval.py --images eval/images --labels eval/labels.csv \\
      --models <free-vision-slug-1> <free-vision-slug-2> --mode hatchback --delay 4

Free OpenRouter models are rate limited. Results are cached in eval/cache.jsonl, so if you hit a
limit just re-run later: finished calls are skipped. Writes eval/results.json (the slide reads it)."""
import argparse, csv, hashlib, json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
from common import geometry, safety, perception, llm  # noqa: E402

DIRECT_PROMPT = ('Estimate the depth of the flood water in this photo in centimetres. Reply with ONLY this JSON: '
                 '{"image_usable": true, "depth_cm_low": 0, "depth_cm_high": 0, "confidence": 0.0}')
BAND_NAMES = [b[2] for b in geometry.BANDS]
BUCKETS = [("<0.4", 0, .4), ("0.4-0.6", .4, .6), ("0.6-0.8", .6, .8), (">=0.8", .8, 1.01)]


def call(variant, model, img, key):
    prompt = DIRECT_PROMPT if variant == "direct" else perception.PROMPT
    raw, used = llm.chat_json(prompt, img, [model], key=key, budget=60)
    return {"raw": raw, "model_used": used}


def interpret(variant, c):
    """Pure post-processing of the cached raw output: editing the geometry table or limits needs no new API calls."""
    raw, used = c["raw"], c["model_used"]
    try:
        if variant == "direct":
            usable, flow = bool(raw.get("image_usable", True)), "still"
            lo, hi = sorted([float(raw["depth_cm_low"]), float(raw["depth_cm_high"])])
            conf = float(raw.get("confidence", 0))
        else:
            obs = perception.sanitize(raw); usable, flow = obs["image_usable"], obs["flow"]
            est = geometry.estimate(obs["objects"], obs["overall_confidence"]) if usable else None
            lo, hi, conf = (est["depth_lo"], est["depth_hi"], est["conf"]) if est else (None, None, 0)
    except (KeyError, ValueError, TypeError, AttributeError):
        return {"abstain": True, "model_used": used}
    if not usable or lo is None:
        return {"abstain": True, "model_used": used}
    return {"abstain": False, "lo": lo, "hi": hi, "conf": round(max(0, min(1, conf)), 2), "flow": flow,
            "band": geometry.band((lo + hi) / 2), "model_used": used}


def score(rows, mode):
    n = len(rows); answered = []; fs = fs_strict = dangerous = dangerous_strict = 0
    conf_mx = {t: {} for t in BAND_NAMES}
    for r in rows:
        p = r["pred"]; d = r["truth_depth"]
        truth_v = safety.verdict(d, d, 1.0, mode)
        pv = "UNKNOWN" if p["abstain"] else safety.verdict(p["lo"], p["hi"], p["conf"], mode, p["flow"])
        r["verdict"] = pv; r["truth_verdict"] = truth_v
        dangerous += truth_v == "DO_NOT_CROSS"; dangerous_strict += truth_v != "GO"
        fs += truth_v == "DO_NOT_CROSS" and pv == "GO"; fs_strict += truth_v != "GO" and pv == "GO"
        if pv != "UNKNOWN":
            answered.append(r)
            cm = conf_mx[r["truth_band"]]; cm[p["band"]] = cm.get(p["band"], 0) + 1
    ok = lambda r: r["truth_band"] == r["pred"]["band"]
    near = lambda r: abs(BAND_NAMES.index(r["truth_band"]) - BAND_NAMES.index(r["pred"]["band"])) <= 1
    by_conf = {}
    for name, a, b in BUCKETS:
        g = [r for r in answered if a <= r["pred"]["conf"] < b]
        by_conf[name] = {"n": len(g), "exact_acc": round(sum(map(ok, g)) / len(g), 3) if g else None}
    rate = lambda x, y: round(x / y, 3) if y else None
    return {"n": n, "answered": len(answered), "abstain_rate": rate(n - len(answered), n),
            "exact_band_acc": rate(sum(map(ok, answered)), len(answered)),
            "within_one_band_acc": rate(sum(map(near, answered)), len(answered)),
            "false_safe_rate": rate(fs, dangerous), "false_safe_count": fs, "dangerous_cases": dangerous,
            "false_safe_rate_strict": rate(fs_strict, dangerous_strict),
            "acc_by_confidence": by_conf, "confusion_truth_x_pred": conf_mx}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", default=os.path.join(HERE, "images"))
    ap.add_argument("--labels", default=os.path.join(HERE, "labels.csv"))
    ap.add_argument("--models", nargs="+", default=["openrouter/free"])
    ap.add_argument("--mode", default="hatchback", choices=list(safety.LIMITS))
    ap.add_argument("--delay", type=float, default=4.0, help="seconds between uncached API calls")
    ap.add_argument("--out", default=os.path.join(HERE, "results.json"))
    a = ap.parse_args()
    key = os.environ.get("OPENROUTER_API_KEY") or sys.exit("Set OPENROUTER_API_KEY")
    labels = list(csv.DictReader(open(a.labels)))
    cache_path = os.path.join(HERE, "cache.jsonl"); cache = {}
    if os.path.exists(cache_path):
        for line in open(cache_path):
            j = json.loads(line); cache[j["k"]] = j["v"]
    results, errors = {}, 0
    for model in a.models:
        for variant in ("direct", "pipeline"):
            rows = []
            for lab in labels:
                img = open(os.path.join(a.images, lab["filename"]), "rb").read()
                ph = hashlib.sha1((DIRECT_PROMPT if variant == "direct" else perception.PROMPT).encode()).hexdigest()[:8]
                k = f"{hashlib.sha1(img).hexdigest()[:16]}|{variant}|{model}|{ph}"   # prompt change => fresh calls
                if k not in cache:
                    try:
                        cache[k] = call(variant, model, img, key)
                        open(cache_path, "a").write(json.dumps({"k": k, "v": cache[k]}) + "\n")
                    except Exception as e:
                        errors += 1; print(f"  ! {lab['filename']} {variant} {model}: {e}"); time.sleep(a.delay); continue
                    time.sleep(a.delay)
                d = float(lab["depth_cm"])
                rows.append({"file": lab["filename"], "truth_depth": d, "truth_band": geometry.band(d),
                             "reference": lab.get("reference_object"), "conditions": lab.get("conditions"), "pred": interpret(variant, cache[k])})
            results[f"{model} | {variant}"] = {"metrics": score(rows, a.mode), "rows": rows}
            m = results[f"{model} | {variant}"]["metrics"]
            print(f"{model:40s} {variant:9s} n={m['n']:3d} exact={m['exact_band_acc']} ±1={m['within_one_band_acc']} "
                  f"false-safe={m['false_safe_rate']} abstain={m['abstain_rate']}")
    json.dump({"mode": a.mode, "bands": BAND_NAMES, "api_errors_this_run": errors, "results": results},
              open(a.out, "w"), indent=2)
    print("wrote", a.out, "(re-run later to fill gaps)" if errors else "")


if __name__ == "__main__":
    main()
