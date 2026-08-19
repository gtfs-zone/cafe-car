from contextvars import ContextVar

# Set per-request by SubjectMiddleware. Needed because SQLAdmin's
# `scaffold_form` has no access to the request. 0 means "nobody", which every
# scoping query treats as matching no rows.
current_user_id_var: ContextVar[int] = ContextVar("current_user_id", default=0)

# Whether the caller is in the Keycloak admin group, which lets the access
# queries in `cafe_car.admin.access` see every feed rather than their own.
#
# Set alongside current_user_id_var and under the same rules, so the two can
# never disagree about who is being answered. The default is False and
# SubjectMiddleware re-sets it on every request, so the only way this is ever
# True is a token whose `sub` matched the proxy header and whose `groups` claim
# carried the admin group.
current_user_is_admin_var: ContextVar[bool] = ContextVar(
    "current_user_is_admin", default=False
)
