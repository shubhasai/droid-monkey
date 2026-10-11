"""Attack plumbing. Every attack, built-in or custom, is a spec run by SpecAttack; built-ins are templates in
droid_monkey/builtin_attacks/ filled per app from the explorer's screen map."""

from .base import Attack, AttackResult, Context
from .spec_attack import SpecAttack

__all__ = ["Attack", "AttackResult", "Context", "SpecAttack"]
