from contextvars import ContextVar

# Set per-request by SubjectMiddleware. Needed because SQLAdmin's
# `scaffold_form` has no access to the request. 0 means "nobody", which every
# scoping query treats as matching no rows.
current_user_id_var: ContextVar[int] = ContextVar("current_user_id", default=0)
