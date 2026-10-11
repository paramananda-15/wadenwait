# Wade or Wait

Photo of a flooded road in, per-vehicle Go / Caution / Do not cross out. Perception (free OpenRouter vision model)
-> geometry (our table) -> safety engine (our limits). Built to stay inside free tiers.

## Layout
- `template.yaml` SAM stack (API Gateway, 6 Lambdas, S3, DynamoDB, hourly hotspot job, $1 budget alert, dashboard)
- `src/common` geometry, safety, perception (OpenRouter), hotspots, geohash, metrics
- `src/handlers` upload_url, analyze, map_pins, report (scheduled), hotspots_api
- `frontend/` single-file PWA (MapLibre + OSM tiles). `eval/` benchmark harness. `tests/` unit tests.

## Deploy (ap-south-1)
1. Free key: https://openrouter.ai/keys
2. `sam build && sam deploy --guided --region ap-south-1`  (enter the key and your email for the budget alert)
3. Put the `ApiUrl` output in `frontend/config.js`, then host `frontend/` on any free static host
   (Cloudflare Pages / GitHub Pages / Amplify drag-and-drop). Camera + GPS need HTTPS, which all of these give you.
4. Hotspot report: `<ApiUrl>/hotspots` (runs hourly; to refresh now: invoke the ReportFn Lambda from the console).
5. Smoke test without any AI: deploy with `PerceptionMode=stub`, then
   `curl -X POST $API/reports -d '{"key":"photos/x/y.jpg","lat":12.97,"lng":77.59,"accuracy":10,"capturedAt":'$(date +%s)',"deviceId":"d1"}'`
   In stub mode no photo or AI call is needed. In normal mode the photo must exist in S3 (upload via the PWA).

## Evaluation
Put 30-50 photos in `eval/images/` and a `eval/labels.csv` (see `labels.example.csv`), then
`OPENROUTER_API_KEY=... python eval/run_eval.py --models <slugA> <slugB> --mode hatchback`.
Pin two specific `:free` vision model slugs from openrouter.ai/models (filter: free + image input) so the comparison is
repeatable; `openrouter/free` picks a random model each call. Output: `eval/results.json`.

## Free-tier notes (verify against your own billing console)
- Free OpenRouter models are rate limited and daily-capped; the harness caches and resumes. Prompts to free models may be
  logged by providers: don't submit private photos.
- DynamoDB uses provisioned 10/10 capacity because on-demand is not in the always-free tier.
- API Gateway REST and S3 free allowances may be time-limited on newer AWS accounts; the budget alert is your safety net.
- CloudWatch: custom metrics from `emf.py` (6 outcomes) fit the 10 free metrics; first 3 dashboards are free.
- Tiles are OpenStreetMap's public tile server: fine for a demo, not for production traffic.

## Object Detector & Weights License (Feature 1)
- Model: `torchvision.models.detection.ssdlite320_mobilenet_v3_large` pre-trained on COCO train2017.
- Model Code License: BSD 3-Clause License (PyTorch / Torchvision).
- Pre-trained Weights License: Trained on the COCO dataset (annotations licensed under Creative Commons Attribution 4.0 International — CC BY 4.0). Weights distributed by PyTorch under permissive open-source terms.
- Supported classes: `car`, `motorcycle`, `bicycle`, `person`, `bus`, `truck`, `stop sign`.
- Perception modes: `PERCEPTION_MODE=vlm` (default, backward-compatible VLM only) or `PERCEPTION_MODE=fusion` (cross-check and interval fusion with detector visible-height estimation and explanation image).

