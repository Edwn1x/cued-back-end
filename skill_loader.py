"""
Skill Loader — Cued
====================
Dynamically loads relevant skills based on message type.
The personality skill is always loaded. Other skills load
only when their triggers match the current message type.

Phase C1 — process-level memoization.
~120 KB of skill markdown was re-read from disk on every coach turn.
We cache parsed skill content in module-level dicts at first load.
Safe because skill files don't mutate at runtime — a deploy is required
to change them, which restarts the process and resets the cache.

The order of SKILL_TRIGGERS dict iteration affects the byte-exact
output of get_skills_for_message_type, which becomes the cache key
for Anthropic prompt caching downstream. DO NOT reorder this dict
without understanding that every cached Block 1 will invalidate.
"""

import os
import logging

logger = logging.getLogger("cued.skills")

SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")

# Map message types to which skills should load.
# ORDERING IS LOAD-BEARING for the Anthropic prompt cache — see module docstring.
SKILL_TRIGGERS = {
    "personality": ["always"],  # always loaded
    "safety": ["always"],  # always loaded
    "training": ["workout_request", "workout_log", "post_workout", "exercise_question", "pre_workout_check"],
    "nutrition": ["meal_suggestion", "meal_swap", "morning_briefing", "craving_report",
                  "dining_question", "photo_of_food", "food_related_message", "meal_check_in"],
    "readiness": ["morning_briefing", "readiness_check"],
    "onboarding": ["new_user_signup"],
}

# Process-level memoization. Skill files are immutable at runtime
# (deploy = process restart = cache reset).
_SKILL_FILE_CACHE: dict[str, str] = {}      # skill_name -> parsed content
_SKILL_BUNDLE_CACHE: dict[str, str] = {}    # message_type -> combined content
_ALL_SKILLS_CACHE: str | None = None        # get_all_skills() fallback


def _strip_yaml_front_matter(text: str) -> str:
    """Remove YAML front matter (---...---) from the top of a skill file."""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            return text[end + 3:].lstrip("\n")
    return text


def load_skill(skill_name):
    """Load a single skill's SKILL.md content, stripping YAML front matter.
    Memoized — first call reads disk, subsequent calls return cached string."""
    cached = _SKILL_FILE_CACHE.get(skill_name)
    if cached is not None:
        return cached
    path = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
    try:
        with open(path, "r") as f:
            content = f.read()
            parsed = _strip_yaml_front_matter(content)
            _SKILL_FILE_CACHE[skill_name] = parsed
            logger.debug(f"Loaded skill: {skill_name}")
            return parsed
    except FileNotFoundError:
        logger.warning(f"Skill not found: {skill_name}")
        _SKILL_FILE_CACHE[skill_name] = ""  # cache the miss too
        return ""


def get_skills_for_message_type(message_type):
    """
    Returns the combined skill content for a given message type.
    Always includes the personality skill.
    Adds other skills whose triggers match the message type.
    Memoized per message_type.
    """
    cached = _SKILL_BUNDLE_CACHE.get(message_type)
    if cached is not None:
        return cached

    skills = []

    # Always load personality
    personality = load_skill("personality")
    if personality:
        skills.append(personality)

    # Load additional skills based on message type — order follows SKILL_TRIGGERS
    # insertion order (load-bearing for the Anthropic cache key).
    for skill_name, triggers in SKILL_TRIGGERS.items():
        if skill_name == "personality":
            continue  # already loaded
        if "always" in triggers or message_type in triggers:
            skill_content = load_skill(skill_name)
            if skill_content:
                skills.append(skill_content)

    combined = "\n\n---\n\n".join(skills)
    _SKILL_BUNDLE_CACHE[message_type] = combined
    logger.info(f"Loaded {len(skills)} skills for message_type={message_type}")
    return combined


def get_all_skills():
    """Load all skills combined. Used as fallback for freeform messages.
    Memoized as a single module-level string."""
    global _ALL_SKILLS_CACHE
    if _ALL_SKILLS_CACHE is not None:
        return _ALL_SKILLS_CACHE

    skills = []
    for skill_name in SKILL_TRIGGERS.keys():
        if skill_name == "onboarding":
            continue  # onboarding has its own agent
        skill_content = load_skill(skill_name)
        if skill_content:
            skills.append(skill_content)

    _ALL_SKILLS_CACHE = "\n\n---\n\n".join(skills)
    return _ALL_SKILLS_CACHE


def list_available_skills():
    """List all available skills in the skills directory."""
    available = []
    if os.path.exists(SKILLS_DIR):
        for name in os.listdir(SKILLS_DIR):
            skill_path = os.path.join(SKILLS_DIR, name, "SKILL.md")
            if os.path.exists(skill_path):
                available.append(name)
    return available
