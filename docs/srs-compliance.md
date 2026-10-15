# SRS compliance and honest limits

Legend: ✅ implemented and tested · 🟡 implemented, but depends on external credentials/services that could not be exercised end-to-end during development · ⚠️ inherent limit.

| § | Requirement | Status | Where / notes |
|---|---|---|---|
| 2 | Video, playlist, channel input; metadata; classification; dedupe; sync; states; coverage ≠ discovered | ✅ (🟡 live YouTube) | `sources.py`, `youtube.py`, `classify.py`; tested with stubbed yt-dlp. Restricted media → *awaiting permission* + authorized upload (no bypass) |
| 3 | Speech + chart analysis, Hindi/English/Hinglish, scene detection, audio–visual alignment | 🟡 | `pipeline.py`, `ai.py`, ffmpeg scene changes; quality depends on Gemini and video resolution ⚠️ |
| 4–5 | Knowledge types, strategy identification, library ranking | ✅ | `library.py`, `grounding.py` |
| 6 | A–Z manual (all sections), examples kept separate | ✅ | manual schema + sanitiser; missing items reported |
| 7 | Provenance, conflicts, edit/approve/freeze, version log | ✅ | `versions.py`, editor |
| 8 | Grounded multi-turn Q&A, evidence links, source vs general | ✅ (🟡 live Gemini) | `qa.py`, `retrieval.py` (BM25 + embeddings) |
| 9 | Readiness classification, approved assumptions versioned | ✅ | `versions.classify_readiness` |
| 10 | Any period, parameters, contract/currency handling, data integrity | ✅ | `engine/`, `instruments.py`, `data.py` quality report |
| 11 | No lookahead, same-bar SL/TP, publication dates, validation | ✅ | `validate.py`; tests 5–9 |
| 12 | Pine generation, audit, two workflows, TradingView comparison | ✅ generation/audit/import · 🟡 compile | The generated script must be compiled in TradingView (no offline Pine compiler). Native TV testing is manual by design (no official API) |
| 13–14 | Journal fields, explanations, chart evidence, metrics, segments, dashboard | ✅ | `journal.py`, `chart.py`, `metrics.py`, run page |
| 15 | Sheets / Excel / CSV, idempotent upsert, 5 sheets | ✅ Excel/CSV · 🟡 Sheets live | Sheets tested against a faithful fake API; needs your OAuth client |
| 16 | Comparison, optimisation as labelled versions, out-of-sample, walk-forward | ✅ | `lab.py` |
| 17–18 | Premium responsive UI, 10 sections, user journey | ✅ | browser QA at 1440 px and 390 px, dark mode |
| 19 | Modular architecture | ✅ | Next.js/Postgres were *suggestions*; FastAPI + SQLite chosen for single-VPS simplicity. Providers/models are swappable |
| 20 | Reliability, security, audit trail | ✅ | checkpoint/resume, retries, auth, per-user isolation, encryption, backups, per-run audit trail |
| 21 | Acceptance tests 1–10 | ✅ 5–10 automated · 🟡 1–4 need real videos | run 1–4 on authorized content with your keys |

⚠️ No system can guarantee perfect extraction or that every historical fill was error-free. The design goal is that every error mode is *detected and disclosed* instead of hidden.
