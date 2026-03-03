from contextvars import ContextVar

current_subject_var: ContextVar[str] = ContextVar("current_subject", default="")
