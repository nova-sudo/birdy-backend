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

# Mistral
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-medium-latest")

# Shared
# Temperature kept low because Birdy is a data-grounded assistant — creative
# prose would be fine with 0.5–0.7, but fabrication risk for numbers/names
# climbs sharply past ~0.3. Hallucinated revenue figures look convincing at
# 0.5+.
DEFAULT_TEMPERATURE = 0.15
MAX_TOOL_ITERATIONS = 5
MAX_RESULT_CHARS = 8000
MAX_RESULT_ITEMS = 20
