"""
Row-level data scope for the read endpoints (getCustomers, getProjects,
getPoles, getPoleVitals, getPoleVitalsByPeriod, getUsers).

A valid token only proves WHO is calling; this decides WHAT they may see.
scope_for() turns the verified AuthContext into a DataScope, and each
query module (customers_api, projects_api, users_api, poles_api,
pole_vitals_api) adds the matching SQL conditions via the *_filter()
helpers below.

    Streetleaf Admin     everything (UNRESTRICTED)
    Customer Owner /     only their own customer's data (customer_id from
    Customer Admin /     the token -- which for an impersonating
    User                 Streetleaf Admin session is the customer they
                         signed in as)
    Streetleaf Crew      only projects that have an installation schedule
                         (Projects.StickerNumbers IS NOT NULL -- set by
                         loadInstallationSchedule, cleared when a project
                         leaves the schedule), the customers that own at
                         least one such project, and those projects' poles.
                         Users are NOT restricted: Crew sees every user,
                         exactly as before scoping existed.

Enforced IN the SQL (WHERE / the inner TOP subquery), never by filtering
rows after the fact -- a post-filter on top of TOP (N) would silently drop
in-scope rows beyond the first N. As a side effect an out-of-scope
single-resource lookup (a project/pole/user Id from another customer)
simply finds nothing, so the endpoints answer 404 / empty list, exactly as
if it didn't exist, and never confirm that it does.

Fails closed: an unknown role, or a customer-scoped role whose token has no
customer, is refused (403) rather than defaulting to "see everything".
"""

from dataclasses import dataclass

from shared.auth_utils import AuthContext, AuthError

_CUSTOMER_SCOPED_ROLES = ("Customer Owner", "Customer Admin", "User")


@dataclass(frozen=True)
class DataScope:
    customer_id: str | None = None
    scheduled_projects_only: bool = False

    @property
    def is_restricted(self) -> bool:
        return self.customer_id is not None or self.scheduled_projects_only


UNRESTRICTED = DataScope()


def scope_for(ctx: AuthContext) -> DataScope:
    role = ctx.role
    if role == "Streetleaf Admin":
        return UNRESTRICTED
    if role == "Streetleaf Crew":
        return DataScope(scheduled_projects_only=True)
    if role in _CUSTOMER_SCOPED_ROLES:
        if not ctx.customer_id:
            raise AuthError(
                "this account is not associated with a customer", status_code=403
            )
        return DataScope(customer_id=ctx.customer_id)
    raise AuthError("this account's role is not recognized", status_code=403)


def _col(alias: str, column: str) -> str:
    return f"{alias}.{column}" if alias else column


def customer_filter(scope: DataScope, alias: str = "") -> tuple[list, list]:
    """
    Conditions restricting a Customers table (aliased `alias`, or bare
    `Customers` when alias is empty). Returns (conditions, params) --
    conditions are SQL fragments with `?` placeholders for params, to be
    ANDed into a WHERE clause.
    """
    conditions, params = [], []
    if scope.customer_id is not None:
        conditions.append(f"{_col(alias or 'Customers', 'Id')} = ?")
        params.append(scope.customer_id)
    if scope.scheduled_projects_only:
        conditions.append(
            "EXISTS (SELECT 1 FROM Projects sp "
            f"WHERE sp.CustomerId = {_col(alias or 'Customers', 'Id')} "
            "AND sp.StickerNumbers IS NOT NULL)"
        )
    return conditions, params


def project_filter(scope: DataScope, alias: str = "") -> tuple[list, list]:
    """Conditions restricting a Projects table (aliased `alias`, or bare)."""
    conditions, params = [], []
    if scope.customer_id is not None:
        conditions.append(f"{_col(alias, 'CustomerId')} = ?")
        params.append(scope.customer_id)
    if scope.scheduled_projects_only:
        conditions.append(f"{_col(alias, 'StickerNumbers')} IS NOT NULL")
    return conditions, params


def user_filter(scope: DataScope, alias: str = "u") -> tuple[list, list]:
    """
    Conditions restricting a Users table (aliased `alias`). Only
    customer-scoped roles are restricted (to their own customer's users);
    Streetleaf Admin and Streetleaf Crew see all users.
    """
    conditions, params = [], []
    if scope.customer_id is not None:
        conditions.append(f"{_col(alias, 'CustomerId')} = ?")
        params.append(scope.customer_id)
    return conditions, params
