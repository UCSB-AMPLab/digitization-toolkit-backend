from datetime import datetime, timezone
from sqlalchemy import event, inspect as sa_inspect, Column, Integer, String, Text, Date, DateTime, ForeignKey, JSON, CheckConstraint, UniqueConstraint, exists, select, text
from sqlalchemy.orm import Session, relationship, validates, object_session

from app.core.db import Base
from app.models.record import ImmutableFieldError, Record, RecordImage


# A volume's images are named `<filename_stem>-img_<nnnn>_c<n>` (exchange
# contract 1.1, section 3), and `<filename_stem>-img_<nnnn>` doubles as the
# pair id, so it has to fit in record_images.pair_id. The stem's cap is what
# is left of that column once `-img_<nnnn>` is appended: 36 - 9 = 27.
PAIR_ID_MAX_LENGTH = RecordImage.__table__.columns["pair_id"].type.length
FILENAME_INDEX_SUFFIX = "-img_0000"
# Static: it assumes four digits. The capture path must check
# len(stem) + len("-img_") + digits <= 36 itself, since <nnnn> outgrows four.
FILENAME_STEM_MAX_LENGTH = PAIR_ID_MAX_LENGTH - len(FILENAME_INDEX_SUFFIX)

# A lowercase slug: runs of [a-z0-9] joined by single hyphens.
FILENAME_STEM_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"

# The same rule in SQLite's GLOB, which has character classes but no
# repetition: only [a-z0-9-], first and last characters alphanumeric (which
# also refuses the empty string), and no two hyphens together. SQLite has no
# regular-expression operator unless one is registered, and the test database
# is SQLite, so this is the form the suite exercises.
_FILENAME_STEM_GLOB = (
    "filename_stem IS NULL OR ("
    "filename_stem GLOB '[a-z0-9]*' AND filename_stem GLOB '*[a-z0-9]' "
    "AND filename_stem NOT GLOB '*[^a-z0-9-]*' AND filename_stem NOT GLOB '*--*')"
)


# An external_id is never empty and never starts or ends with whitespace: a
# reference code that differs from another only by padding would pass the
# unique index, and an empty one would take the write-once slot. The request
# schemas strip it; these refuse it from any other writer. On SQLite
# "whitespace" is the six characters passed to trim(): space, tab, newline,
# vertical tab, form feed, carriage return. On Postgres it is [[:space:]],
# which covers those six and, depending on the database's locale, may also
# match other Unicode spaces.
_EXTERNAL_ID_TRIMMED_POSTGRES = (
    "external_id IS NULL OR (external_id <> '' "
    "AND external_id !~ '^[[:space:]]' AND external_id !~ '[[:space:]]$')"
)
_EXTERNAL_ID_TRIMMED_SQLITE = (
    "external_id IS NULL OR (external_id <> '' "
    "AND external_id = trim(external_id, char(32, 9, 10, 11, 12, 13)))"
)


class StemReservedError(ImmutableFieldError):
    """A delete would free a stem whose file names have been issued."""


class StemDirectoryCollisionError(ValueError):
    """A stem and a project's directory name would name the same directory under the projects root.

    A volume with a stem keeps its files in `<projects root>/<stem>/`, and a
    project its tree in `<projects root>/<project directory>/`. Raised by the
    before_flush listener _refuse_a_stem_that_is_a_project_directory, so
    every writer meets it; the API answers 409 (app.main).
    """


class StemIsProjectDirectoryError(StemDirectoryCollisionError):
    """The stem is the directory name of a project."""


class StemDirectoryOccupiedError(StemDirectoryCollisionError):
    """`<projects root>/<stem>/` already holds files that are not the volume's."""


class ProjectNameIsStemError(StemDirectoryCollisionError):
    """A project's directory name would be a volume's stem."""


def _stem_directory_is_someone_elses(session, stem, collection) -> bool:
    """Whether `<projects root>/<stem>/` exists with a file in it and `collection` has no image there.

    A directory holding only empty directories is free. Reads the disk under
    the projects root as configured, without the storage-override write
    check, so it never raises for an unusable drive.
    """
    from pathlib import Path

    from app.core import config

    root = config.settings.projects_dir
    directory = root / stem
    try:
        if not directory.is_dir() or not any(p.is_file() for p in directory.rglob("*")):
            return False
    except OSError:
        return True
    if collection is not None and collection.id is not None:
        prefix = (root.resolve() / stem)
        rows = session.execute(
            select(RecordImage.file_path).where(RecordImage.record_id == Record.id,
                                                Record.collection_id == collection.id)
        ).scalars().all()
        for fp in rows:
            try:
                if fp and Path(fp).resolve().is_relative_to(prefix):
                    return False
            except (OSError, ValueError):
                continue
    return True


def stem_directory_conflict(session, stem=None, project_name=None, collection=None):
    """The StemDirectoryCollisionError refusing `stem`, or a project named `project_name`, or None.

    A stem collides with a project whose directory name (project_dir_name)
    equals it (StemIsProjectDirectoryError), and with a
    `<projects root>/<stem>/` that already exists on disk with a file in it
    when `collection` (the volume taking the stem, or None for one not yet
    created) has no image there (StemDirectoryOccupiedError). A project name
    collides with a volume whose stem equals its directory name
    (ProjectNameIsStemError). Reads with autoflush off, so it can run inside
    a flush. The error is returned, not raised.
    """
    from app.core.paths import project_dir_name
    from app.models.project import Project

    with session.no_autoflush:
        if stem is not None:
            for (name,) in session.query(Project.name).all():
                if project_dir_name(name) == stem:
                    return StemIsProjectDirectoryError(
                        f"The file name stem {stem} is the directory of project {name!r}; "
                        "a volume's files and a project's tree cannot share it.")
            if _stem_directory_is_someone_elses(session, stem, collection):
                return StemDirectoryOccupiedError(
                    f"The directory {stem} already exists in the projects root and holds files that are "
                    "not this volume's; a volume's files are never merged into it.")
        if project_name is not None:
            directory = project_dir_name(project_name)
            if session.query(Collection.id).filter(Collection.filename_stem == directory).first() is not None:
                return ProjectNameIsStemError(
                    f"The project name {project_name!r} would use the directory {directory}, "
                    "which is a volume's file name stem.")
    return None


def stem_directory_collision(session, stem=None, project_name=None, collection=None):
    """The message of stem_directory_conflict, or None when there is no collision."""
    error = stem_directory_conflict(session, stem=stem, project_name=project_name, collection=collection)
    return str(error) if error is not None else None


STEM_RESERVED_MESSAGE = (
    "This volume has a file name stem and has captured images, so it cannot be "
    "deleted; it keeps its stem so the name is never issued again."
)


class Collection(Base):
    __tablename__ = "collections"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, index=True)
    description = Column(Text, nullable=True)
    
    # Type of collection (e.g., "fonds", "series", "box", "folder", "volume")
    collection_type = Column(String(50), nullable=True)
    
    # Parent relationships - either under a project OR another collection
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True)
    parent_collection_id = Column(Integer, ForeignKey("collections.id", ondelete="CASCADE"), nullable=True)
    
    # Metadata for archival-specific fields (flexible JSON)
    archival_metadata = Column(JSON, nullable=True)

    # The volume's identity under exchange contract 1.1, which maps each
    # Fisqua item onto a volume (this table, "Volumen" in the interface).
    #
    # filename_stem: the slug its image file names are built from. Nullable,
    # because existing volumes have none until an operator assigns one, and
    # nothing generates one. Unique, so two volumes cannot produce the same
    # file names. Pattern and length are CHECKs (see __table_args__); the
    # column is wider than the cap so that an overlong stem is refused by the
    # named length CHECK on every engine, rather than by Postgres's varchar
    # limit as a different kind of error. Fixed once it is set and the volume
    # has captured — see _filename_stem_is_fixed_by_the_first_capture.
    filename_stem = Column(String(255), nullable=True)

    # external_id: Fisqua's reference code for the item. Unique through one
    # unique index, which is also the lookup the exchange uses. Written once —
    # see _external_id_is_write_once.
    external_id = Column(String(255), nullable=True, unique=True, index=True)

    # Descriptive fields, mirroring the same columns on records: a free-text
    # display date beside a queryable range, an order, and a Text column
    # holding encoded JSON (where the contract parks Fisqua fields that have
    # no column, under a `fisqua` key).
    date = Column(String(50), nullable=True)
    date_start = Column(Date, nullable=True)
    date_end = Column(Date, nullable=True)
    sequence = Column(Integer, nullable=True)
    custom_attributes = Column(Text, nullable=True)

    # Every capture the volume has ever had, superseded ones included (exchange
    # contract 1.1, section 3): the source of `<nnnn>` in its file names, so a
    # number is never reused. It never decreases — see
    # _capture_counter_never_decreases — and a CHECK refuses a negative value.
    # next_capture_number (app/core/capture_counter.py) raises it, once per
    # capture event: each single or dual capture, each retake, and each upload
    # through POST /records/{id}/images.
    capture_counter = Column(Integer, nullable=False, default=0, server_default=text("0"))

    created_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    project = relationship("Project", back_populates="collections")
    parent_collection = relationship("Collection", remote_side=[id], back_populates="child_collections")
    child_collections = relationship("Collection", back_populates="parent_collection", cascade="all, delete-orphan")
    records = relationship("Record", back_populates="collection")

    @validates("external_id", include_removes=True)
    def _external_id_is_write_once(self, key, value, is_remove):
        """Refuse any assignment or removal that would change a stored external_id.

        A reference code that can be reassigned makes every trace drawn through
        it unverifiable. Assigning the identical value is allowed; clearing to
        NULL or removing the attribute is a change and is refused. With nothing
        stored, a removal takes nothing away and is allowed.

        An ORM attribute guard: `query().update()` and raw SQL go around it.
        """
        current = self.external_id
        if is_remove:
            if current is not None:
                raise ImmutableFieldError(
                    f"collections.external_id is written once and cannot be removed "
                    f"(volume {self.id}: {current!r})"
                )
            return value
        if current is not None and value != current:
            raise ImmutableFieldError(
                f"collections.external_id is written once and cannot be changed "
                f"(volume {self.id}: {current!r} -> {value!r})"
            )
        return value

    def has_images(self) -> bool:
        """Whether any image, superseded or current, belongs to a record in this volume.

        Images reach a volume only through records: record_images.record_id ->
        records.collection_id. Only the volume's own records count, not those of
        volumes nested inside it, since each record's files are named from its
        own volume's stem. Superseded images (is_current False) count, because
        a rejected file is kept under its name rather than deleted.

        Two sources: images pending in the session (added and not yet
        flushed), whose record is in this volume by collection_id or by the
        `collection` relationship; then the database, read with autoflush
        off so that the check itself flushes nothing. A pending image counts
        so that a stem change in the same flush as the volume's first image
        is refused. A volume that is not yet persisted has no id and no
        images.
        """
        if self.id is None:
            return False
        session = object_session(self)
        if session is None:
            return False
        with session.no_autoflush:
            for obj in session.new:
                if isinstance(obj, RecordImage) and self._holds_record(session, obj):
                    return True
            return session.execute(
                select(exists().where(
                    RecordImage.record_id == Record.id,
                    Record.collection_id == self.id,
                ))
            ).scalar()

    def _holds_record(self, session, image) -> bool:
        """Whether a pending image's record, pending or stored, is in this volume."""
        record = image.record
        if record is None and image.record_id is not None:
            record = session.get(Record, image.record_id)
        if record is None:
            return False
        return record.collection is self or record.collection_id == self.id

    def has_captured(self) -> bool:
        """Whether the volume has ever captured: capture_counter above zero, or any image.

        The counter keeps the answer after the volume's records are deleted or
        moved out. next_capture_number raises it for each capture and upload
        into the volume; the image check covers images that reach the volume
        without one, such as those on a record moved in from another volume.
        """
        return (self.capture_counter or 0) > 0 or self.has_images()

    def reserves_its_stem(self) -> bool:
        """Whether the volume has a stem and has captured, so its row must be kept.

        Deleting such a volume would free the stem under the unique
        constraint, and a later volume could take it and issue the same file
        names again. A volume without a stem, or with one it has not yet
        captured under, reserves nothing.
        """
        return self.filename_stem is not None and self.has_captured()

    @property
    def stem_reserved(self) -> bool:
        """reserves_its_stem() as an attribute, for CollectionRead."""
        return self.reserves_its_stem()

    @validates("capture_counter", include_removes=True)
    def _capture_counter_never_decreases(self, key, value, is_remove):
        """Refuse any write that would lower or clear a stored capture_counter.

        A number once given to a capture is never given again, so the counter
        can only rise. Raising it and rewriting the same value are allowed.
        With nothing stored (a volume not yet flushed) any value is allowed.

        An ORM attribute guard: `query().update()` and raw SQL go around it,
        apart from the CHECK refusing a negative value.
        """
        current = self.capture_counter
        if is_remove:
            if current is not None:
                raise ImmutableFieldError(
                    f"collections.capture_counter never decreases and cannot be removed "
                    f"(volume {self.id}: {current!r})"
                )
            return value
        if current is not None and (value is None or value < current):
            raise ImmutableFieldError(
                f"collections.capture_counter never decreases "
                f"(volume {self.id}: {current!r} -> {value!r})"
            )
        return value

    @validates("filename_stem", include_removes=True)
    def _filename_stem_is_fixed_by_the_first_capture(self, key, value, is_remove):
        """Refuse a change to a set filename_stem once the volume has captured.

        Before the first capture the stem is free: set, changed or cleared.
        A NULL stem may be set once whether or not the volume has captured,
        because that is how a volume captured before stems existed gets one
        from an operator. Once a stem is set and the volume has captured
        (has_captured), files exist or have existed whose names were built
        from it, so any other value — a different stem, NULL, or a removal —
        is refused. That holds after the volume's records are deleted or
        moved out, because the counter does not go down. Assigning the
        identical value is allowed, since it changes nothing.

        An ORM attribute guard, like the external_id one: `query().update()`
        and raw SQL go around it.
        """
        current = self.filename_stem
        if is_remove:
            if current is not None and self.has_captured():
                raise ImmutableFieldError(
                    f"collections.filename_stem is fixed once the volume has captured "
                    f"and cannot be removed (volume {self.id}: {current!r})"
                )
            return value
        if current is not None and value != current and self.has_captured():
            raise ImmutableFieldError(
                f"collections.filename_stem is fixed once the volume has captured "
                f"(volume {self.id}: {current!r} -> {value!r})"
            )
        return value

    __table_args__ = (
        # Constraint: must have either project_id OR parent_collection_id (but not both)
        CheckConstraint(
            '(project_id IS NOT NULL AND parent_collection_id IS NULL) OR (project_id IS NULL AND parent_collection_id IS NOT NULL)',
            name='check_collection_parent'
        ),
        # An end before its start, as check_record_date_range on records.
        CheckConstraint(
            'date_start IS NULL OR date_end IS NULL OR date_end >= date_start',
            name='check_collection_date_range'
        ),
        CheckConstraint(
            'capture_counter >= 0',
            name='check_collection_capture_counter'
        ),
        CheckConstraint(
            _EXTERNAL_ID_TRIMMED_POSTGRES,
            name='check_collection_external_id_trimmed'
        ).ddl_if(dialect='postgresql'),
        CheckConstraint(
            _EXTERNAL_ID_TRIMMED_SQLITE,
            name='check_collection_external_id_trimmed'
        ).ddl_if(dialect='sqlite'),
        UniqueConstraint('filename_stem', name='collections_filename_stem_key'),
        # length() counts characters on both Postgres and SQLite.
        CheckConstraint(
            f'filename_stem IS NULL OR length(filename_stem) <= {FILENAME_STEM_MAX_LENGTH}',
            name='check_collection_filename_stem_length'
        ),
        # The pattern has one name and two spellings, one per engine: a POSIX
        # regex on Postgres, the GLOB form above on SQLite. Each is emitted
        # only on its own engine.
        CheckConstraint(
            f"filename_stem IS NULL OR filename_stem ~ '{FILENAME_STEM_PATTERN}'",
            name='check_collection_filename_stem_pattern'
        ).ddl_if(dialect='postgresql'),
        CheckConstraint(
            _FILENAME_STEM_GLOB,
            name='check_collection_filename_stem_pattern'
        ).ddl_if(dialect='sqlite'),
    )


# The delete-orphan relationships whose target is a volume: removing a volume
# from one of these lists deletes it at the next flush unless it has been
# given another parent.
_ORPHANING_RELATIONSHIPS = ("child_collections", "collections")


def _orphaned_volumes(session):
    """Volumes removed from a delete-orphan parent list and not re-parented.

    Read from each dirty parent's relationship history: the ORM decides
    orphans during the flush, after before_flush, so they are not yet in
    session.deleted. A volume that has a parent again by either relationship
    (another collection, or a project) in the same flush is not an orphan.
    """
    for parent in list(session.dirty):
        state = sa_inspect(parent)
        for name in _ORPHANING_RELATIONSHIPS:
            rel = state.mapper.relationships.get(name)
            if rel is None or rel.mapper.class_ is not Collection:
                continue
            for child in state.attrs[name].history.deleted:
                if child.parent_collection is None and child.project is None:
                    yield child


@event.listens_for(Session, "before_flush")
def _refuse_to_delete_a_reserved_stem(session, flush_context, instances):
    """Refuse a flush that would delete a volume that reserves its stem.

    DELETE /collections/{id} refuses this itself with a 409. This covers the
    ORM paths the API does not use:
    - a direct session.delete(), and the delete cascades from
      Project.collections and Collection.child_collections, which put the
      volume in session.deleted when session.delete() is called, before the
      flush;
    - a delete-orphan, where a volume is removed from one of those lists.
      The ORM finds orphans during the flush, after this listener has run,
      so such a volume is not in session.deleted here; _orphaned_volumes
      reads the removals from the parents' history instead.

    Decided before the flush rather than during it: Collection.records has
    no cascade, so the flush that deletes a volume first sets
    records.collection_id to NULL on its records, and an image check made
    after that (a before_delete hook) finds none. Here the records still
    point at the volume. A database-level ON DELETE CASCADE or raw SQL goes
    around it.
    """
    doomed = [obj for obj in list(session.deleted) if isinstance(obj, Collection)]
    doomed.extend(_orphaned_volumes(session))
    for obj in doomed:
        if obj.reserves_its_stem():
            raise StemReservedError(
                f"collections.filename_stem is reserved: volume {obj.id} has captured "
                f"under it and cannot be deleted"
            )


@event.listens_for(Session, "before_flush")
def _refuse_a_stem_that_is_a_project_directory(session, flush_context, instances):
    """Refuse a flush that gives a volume a stem equal to a project's directory, or the reverse.

    Covers every writer: the collection and project routes, and the list
    import, which creates volumes through the model. Only a new volume, a
    changed stem, a new project or a renamed project is checked. Because of
    the stem pattern, only a project name that folds to a lowercase
    hyphenated slug can collide. Two writes in one flush that collide with
    each other (a new project and a new volume) are compared too.
    """
    from app.core.paths import project_dir_name
    from app.models.project import Project

    pending = list(session.new) + list(session.dirty)
    new_stems = {}
    new_dirs = {}
    for obj in pending:
        if isinstance(obj, Collection) and obj.filename_stem is not None:
            if obj in session.new or sa_inspect(obj).attrs.filename_stem.history.has_changes():
                new_stems[obj.filename_stem] = obj
        elif isinstance(obj, Project) and obj.name:
            if obj in session.new or sa_inspect(obj).attrs.name.history.has_changes():
                new_dirs[project_dir_name(obj.name)] = obj
    for stem, volume in new_stems.items():
        error = stem_directory_conflict(session, stem=stem, collection=volume)
        if error is None and stem in new_dirs:
            error = StemIsProjectDirectoryError(
                f"The file name stem {stem} is the directory of project {new_dirs[stem].name!r}; "
                "a volume's files and a project's tree cannot share it.")
        if error is not None:
            raise error
    for directory, project in new_dirs.items():
        error = stem_directory_conflict(session, project_name=project.name)
        if error is not None:
            raise error
