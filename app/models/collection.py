from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Date, DateTime, ForeignKey, JSON, CheckConstraint, UniqueConstraint, exists, select, text
from sqlalchemy.orm import relationship, validates, object_session

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
    # Nothing raises it yet; the capture path will.
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

        The same rule, and the same reasons, as Record._external_id_is_write_once:
        a reference code that can be reassigned makes every trace drawn through
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
        moved out; the image check covers images the counter does not yet
        count, since nothing raises it until the capture path does.
        """
        return (self.capture_counter or 0) > 0 or self.has_images()

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
