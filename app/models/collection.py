from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Date, DateTime, ForeignKey, JSON, CheckConstraint, UniqueConstraint, exists, select
from sqlalchemy.orm import relationship, validates, object_session

from app.core.db import Base
from app.models.record import ImmutableFieldError, Record, RecordImage


# A volume's images are named `<filename_stem>-img_<nnnn>_c<n>` (exchange
# contract 1.1, section 3), and `<filename_stem>-img_<nnnn>` doubles as the
# pair id, so it has to fit in record_images.pair_id. The stem's cap is what
# is left of that column once `-img_<nnnn>` is appended: 36 - 9 = 27.
PAIR_ID_MAX_LENGTH = RecordImage.__table__.columns["pair_id"].type.length
FILENAME_INDEX_SUFFIX = "-img_0000"
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
    # limit as a different kind of error. Fixed once the volume has an image —
    # see _filename_stem_is_fixed_by_the_first_image.
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

        Reads the database with autoflush off, so an image added to the session
        and not yet flushed is not seen. A volume that is not yet persisted has
        no id and no images.
        """
        if self.id is None:
            return False
        session = object_session(self)
        if session is None:
            return False
        with session.no_autoflush:
            return session.execute(
                select(exists().where(
                    RecordImage.record_id == Record.id,
                    Record.collection_id == self.id,
                ))
            ).scalar()

    @validates("filename_stem", include_removes=True)
    def _filename_stem_is_fixed_by_the_first_image(self, key, value, is_remove):
        """Refuse a change to filename_stem once the volume holds an image.

        Until then the stem is free: set, changed or cleared. From the first
        image on, files exist whose names were built from the stem in force
        when they were written, so any other value — a different stem, NULL,
        a removal, or a first stem on a volume that captured without one —
        would leave those files named from something the volume no longer
        says. Assigning the identical value is allowed, since it changes
        nothing.

        An ORM attribute guard, like the external_id one: `query().update()`
        and raw SQL go around it. "Has an image" is has_images().
        """
        current = self.filename_stem
        if is_remove:
            if current is not None and self.has_images():
                raise ImmutableFieldError(
                    f"collections.filename_stem is fixed once the volume has an image "
                    f"and cannot be removed (volume {self.id}: {current!r})"
                )
            return value
        if value != current and self.has_images():
            raise ImmutableFieldError(
                f"collections.filename_stem is fixed once the volume has an image "
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
