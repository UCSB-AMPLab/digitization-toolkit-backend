# Version: 0.1.0-beta

from datetime import datetime, timezone
from sqlalchemy import Boolean, CheckConstraint, Column, Integer, String, Text, DateTime, event, false, inspect as sa_inspect
from sqlalchemy.orm import Session, relationship, validates

from app.core.db import Base
from app.models.record import ImmutableFieldError


class TestingSpaceError(ImmutableFieldError):
    """A write would put testing and real work together (app.core.testing_space).

    Raised for a change of `testing` after insert, on a project or a volume;
    for a volume whose `testing` is not its project's or its parent's; for a
    testing volume no longer kept on the unit; and, with `code`
    `name_reserved`, for a real project's name, or a real volume's stem or
    reference code, in the testing space's namespace, and `testing_name_required`
    for a testing one outside it (app.core.project_names.TESTING_PREFIX).
    `code` is None for the others.
    """

    def __init__(self, message: str, code=None, field=None):
        super().__init__(message)
        self.code = code
        # The column refused, as `table.column`, where one is.
        self.field = field


def refuse_testing_change(obj, value, is_remove):
    """The shared `testing` validator: free before insert, fixed afterwards.

    Before the row is flushed any value may be set. Once it has an identity,
    a different value or a removal raises TestingSpaceError; the identical
    value is allowed, since it changes nothing. An ORM attribute guard:
    `query().update()` and raw SQL go around it.
    """
    if not sa_inspect(obj).has_identity:
        return value
    current = obj.testing
    if is_remove or bool(value) != bool(current):
        raise TestingSpaceError(
            f"{obj.__tablename__}.testing is set at insert and never changes "
            f"({obj.__tablename__} {obj.id}: {current!r} -> {value!r})")
    return value


# A testing project's capture format is one of capture.backends.base.IMAGE_FORMATS;
# an ordinary project has none. `IS NOT NULL` is spelled out because a NULL
# makes `IN` unknown, and a CHECK that is unknown passes. Also the migration's CHECK
# (alembic/versions/c8e2a4f6b0d1_add_testing_space.py).
CAPTURE_FORMAT_CHECK = (
    "(testing AND capture_format IS NOT NULL AND capture_format IN ('JPEG','RAW+JPEG')) OR (NOT testing AND capture_format IS NULL)")
# The format a testing project takes when none is chosen.
DEFAULT_TESTING_CAPTURE_FORMAT = "JPEG"


def _default_capture_format(context):
    """The insert default of projects.capture_format: JPEG for a testing project, none for an ordinary one."""
    return DEFAULT_TESTING_CAPTURE_FORMAT if context.get_current_parameters().get("testing") else None


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(CAPTURE_FORMAT_CHECK, name="check_project_capture_format"),
    )

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, index=True, nullable=False)
    description = Column(Text, nullable=True)
    fondo = Column(String(512), nullable=True)
    serie = Column(String(512), nullable=True)
    signatura = Column(String(255), nullable=True)
    created_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    # A testing project (app.core.testing_space): it holds testing volumes
    # only, and is never the default project or a list import's. Set at
    # insert and never changed (_testing_is_fixed_at_insert). Its name
    # begins with app.core.project_names.TESTING_PROJECT_PREFIX, and a real
    # project's never does (_a_projects_name_is_in_its_spaces_namespace).
    testing = Column(Boolean, nullable=False, default=False, server_default=false())
    # The capture format every capture into the project's volumes takes, "JPEG"
    # or "RAW+JPEG", on a testing project; None on an ordinary project
    # (check_project_capture_format). It wins over the format the unit stores
    # for the camera body, for those captures only (app.api.cameras._put_capture_format).
    capture_format = Column(String(16), nullable=True, default=_default_capture_format)

    records = relationship("Record", back_populates="project")
    collections = relationship("Collection", back_populates="project", cascade="all, delete-orphan")

    @validates("testing", include_removes=True)
    def _testing_is_fixed_at_insert(self, key, value, is_remove):
        return refuse_testing_change(self, value, is_remove)

    @property
    def display_name(self):
        """The name every response gives for the project: the stored name without the testing prefix."""
        from app.core.project_names import display_project_name

        return display_project_name(self.name)


@event.listens_for(Session, "before_flush")
def _a_projects_name_is_in_its_spaces_namespace(session, flush_context, instances):
    """Refuse a flush that inserts or renames a project with a name outside its space's namespace.

    A testing project's name begins with app.core.project_names.TESTING_PROJECT_PREFIX
    (code `testing_name_required`); a real project's name, and its
    directory name (project_dir_name), never do (code `name_reserved`).
    Checked on the values the flush will write, whatever order the
    attributes were set in. `query().update()` and raw SQL go around it.
    """
    from app.core import testing_space

    for obj in list(session.new) + list(session.dirty):
        if not isinstance(obj, Project):
            continue
        if obj not in session.new and not sa_inspect(obj).attrs.name.history.has_changes():
            continue
        code = testing_space.namespace_problem(bool(obj.testing), obj.name, project=True)
        if code is not None:
            raise TestingSpaceError(f"{testing_space.NAMESPACE_MESSAGES[code]} (projects.name)", code=code,
                                    field="projects.name")
