"""
Skill Loader — Cued
====================
Dynamically loads relevant skills based on message type.
The personality skill is always loaded. Other skills load
only when their triggers match the current message type.
"""

import os
import logging

logger = logging.getLogger("cued.skills")

SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")

# Map message types to which skills should load
SKILL_TRIGGERS = {
    "personality": ["always"],  # always loaded
    "safety": ["always"],  # always loaded
    "training": ["workout_request", "workout_log", "post_workout"],
    "nutrition": ["meal_suggestion", "meal_swap", "morning_briefing"],
    "readiness": ["morning_briefing", "readiness_check"],
    "onboarding": ["new_user_signup"],
}


def load_skill(skill_name):
    """Load a single skill's SKILL.md content."""
    path = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
    try:
        with open(path, "r") as f:
            content = f.read()
            logger.debug(f"Loaded skill: {skill_name}")
            return content
    except FileNotFoundError:
        logger.warning(f"Skill not found: {skill_name}")
        return ""


def get_skills_for_message_type(message_type):
    """
    Returns the combined skill content for a given message type.
    Always includes the personality skill.
    Adds other skills whose triggers match the message type.
    """
    skills = []
    
    # Always load personality
    personality = load_skill("personality")
    if personality:
        skills.append(personality)
    
    # Load additional skills based on message type
    for skill_name, triggers in SKILL_TRIGGERS.items():
        if skill_name == "personality":
            continue  # already loaded
        if "always" in triggers or message_type in triggers:
            skill_content = load_skill(skill_name)
            if skill_content:
                skills.append(skill_content)
    
    combined = "\n\n---\n\n".join(skills)
    logger.info(f"Loaded {len(skills)} skills for message_type={message_type}")
    return combined


def get_all_skills():
    """Load all skills combined. Used as fallback for freeform messages."""
    skills = []
    for skill_name in SKILL_TRIGGERS.keys():
        if skill_name == "onboarding":
            continue  # onboarding has its own agent
        skill_content = load_skill(skill_name)
        if skill_content:
            skills.append(skill_content)
    
    return "\n\n---\n\n".join(skills)


def list_available_skills():
    """List all available skills in the skills directory."""
    available = []
    if os.path.exists(SKILLS_DIR):
        for name in os.listdir(SKILLS_DIR):
            skill_path = os.path.join(SKILLS_DIR, name, "SKILL.md")
            if os.path.exists(skill_path):
                available.append(name)
    return available
