import os

# Provider selection: "mistral", "groq", "anthropic", or "openrouter"
AI_PROVIDER = os.getenv("AI_PROVIDER", "mistral")

# Groq
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

# OpenRouter
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "qwen/qwen3.6-plus:free")

# OpenAI — the account-wide key every user's chat runs on. Birdy supplies the
# model rather than asking each agency for their own: a user pasting the wrong
# key, or a key for a model that cannot call tools, silently degrades the
# product in ways they cannot diagnose.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

# Mistral
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-medium-latest")

# Shared
# Temperature kept low because Birdy is a data-grounded assistant — creative
# prose would be fine with 0.5–0.7, but fabrication risk for numbers/names
# climbs sharply past ~0.3. Hallucinated revenue figures look convincing at
# 0.5+.
# DEFAULT_TEMPERATURE kept low, see above. MAX_TOOL_ITERATIONS was 5 — too low
# for the multi-tool-call patterns ai/prompts/birdy.py itself instructs (e.g. a
# "last 30/60/90 days" breakdown needs a live call per non-cached window per
# data source, 6+ calls, since only 13 fixed presets are cached — no 60d/90d
# preset exists). Hitting the cap forced a no-tools final turn with no signal
# to the model that it was cut short, which produced fabricated data under
# user pushback (see ai/orchestrator.py's final-iteration notice, the paired
# fix for this same incident).
DEFAULT_TEMPERATURE = 0.15
MAX_TOOL_ITERATIONS = 12
MAX_RESULT_CHARS = 8000
MAX_RESULT_ITEMS = 20

# ── Call analysis (Whisper transcription) ───────────────────────────────────
# Runs on the account OPENAI_API_KEY regardless of the chat provider — the
# chat model may be Mistral/Groq/etc., but transcription is always OpenAI.
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")
# Cheap model used inside the analyze tool to condense each transcript before
# it reaches the chat model (raw transcripts would blow MAX_RESULT_CHARS).
CALL_ANALYSIS_SUMMARY_MODEL = os.getenv("CALL_ANALYSIS_SUMMARY_MODEL", "gpt-4o-mini")
# Hard cap per analysis run. Analysis is synchronous inside the chat request
# (Vercel serverless — no long-lived workers), so the batch must stay small;
# the agent offers a "next batch" instead of one huge run.
CALL_ANALYSIS_MAX_CALLS = 15

# ── BYOK curated model lists ────────────────────────────────────────────────
# Only models known to support tool/function calling. Users pick one of
# these when connecting their own Anthropic/OpenAI key (see
# routers/ai_credentials.py); the exact choice is still live-validated at
# save time (services/ai_credential_service.py), so a stale entry here just
# means that specific save gets rejected with a clear message, not silent
# breakage. Review periodically as providers ship new models.
AVAILABLE_AI_MODELS = {
    "anthropic": [
        {"id": "claude-sonnet-5", "label": "Claude Sonnet 5"},
        {"id": "claude-opus-4-8", "label": "Claude Opus 4.8"},
        {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5"},
    ],
    "openai": [
        {"id": "gpt-5", "label": "GPT-5"},
        {"id": "gpt-4.1", "label": "GPT-4.1"},
        {"id": "gpt-4o", "label": "GPT-4o"},
    ],
}
