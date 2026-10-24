class RosterError(ValueError):
    """A scheduling rule rejected the requested change."""


class NotFound(RosterError):
    pass


class NotAuthorized(RosterError):
    pass


class NoCapacity(RosterError):
    pass


class MonthlyLimit(RosterError):
    pass


class RoleMismatch(RosterError):
    pass


class RoleConflictError(RosterError):
    pass


class Unavailable(RosterError):
    pass
