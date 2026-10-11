from .base import Attack, AttackResult, Context
from .network_loss import NetworkLoss
from .permission_revoke import PermissionRevoke
from .process_death import ProcessDeath
from .rotation import Rotation

CATALOGUE: dict[str, type[Attack]] = {
    a.name: a for a in (ProcessDeath, Rotation, NetworkLoss, PermissionRevoke)
}

__all__ = ["Attack", "AttackResult", "Context", "CATALOGUE"]
