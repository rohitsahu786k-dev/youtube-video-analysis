# AI Pipeline

## Cost ladder
1. Local transcript rules find obvious candidate moments.
2. Luna semantic pass catches equivalent language missed by keywords.
3. Only selected compressed frames around candidates are sent for vision.
4. Terra performs trade extraction/validation.
5. Sol is reserved for final cross-video synthesis/RAG where needed.

Prompts are versioned in `src/ai/prompts/`. Structured responses use JSON Schema and are validated again with Zod before use.

## Safety against hallucination
- exact prices default to null unless evidence supports them
- spoken claims and visual observations are separate
- second-pass validation identifies unsupported fields
- AI confidence is displayed as an AI score, not a calibrated statistical probability
- profit claims have verification status
