"""Registry of general editorial prompts available to the review CLI."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PromptProfile:
    name: str
    path: Path


DEFAULT_PROMPT_PROFILE = "schoolbook"
PROMPT_PROFILES = {
    "schoolbook": PromptProfile("Libri scolastici", Path(__file__).with_name("prompt.txt")),
}
