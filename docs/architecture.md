# Architecture
YouTube → metadata queue → worker download → FFmpeg audio → timestamped transcription → candidate events → selective compressed frames → multimodal observations → trade extraction → validation → knowledge index → patterns/playbook/RAG.

Next.js handles UI/API only. Redis/BullMQ workers handle long media/AI jobs. MongoDB stores structured evidence and knowledge chunks. Temporary media is removed after processing. Exact unsupported trade values remain null.
