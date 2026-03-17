"""JARVIS-style Skill Engine for G Assistant."""

from skills.engine import Skill, SkillRegistry, JarvisEngine, TaskPlan, TaskStep

# Re-export SkillLibrary and SkillTrainer from the standalone skills.py module
# (skills.py lives at the project root alongside this package)
import importlib.util as _ilu
import os as _os

_skills_py = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "skills.py")
_skills_lib_mod = None
if _os.path.exists(_skills_py):
    _spec = _ilu.spec_from_file_location("_skills_lib", _skills_py)
    _skills_lib_mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_skills_lib_mod)

SkillLibrary  = getattr(_skills_lib_mod, "SkillLibrary",  None)
SkillTrainer  = getattr(_skills_lib_mod, "SkillTrainer",  None)
SKILL_CURRICULUM = getattr(_skills_lib_mod, "SKILL_CURRICULUM", {})
_SKILL_TOOLS  = getattr(_skills_lib_mod, "_SKILL_TOOLS",  [])

__all__ = [
    "Skill", "SkillRegistry", "JarvisEngine", "TaskPlan", "TaskStep",
    "SkillLibrary", "SkillTrainer", "SKILL_CURRICULUM", "_SKILL_TOOLS",
]
