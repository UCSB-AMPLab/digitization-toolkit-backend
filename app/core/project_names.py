"""How project names are stored and compared, and the namespace reserved for the testing space.

Pure functions, with no database or model imports, so that the models
(app.models.project, app.models.collection), the list import
(app.core.list_import) and the routes use the same rule.

- stored_name: stripped, in NFC; the form a list name is stored under.
- name_key: stored_name case-folded; what two project names are compared
  by. Accents are kept: "válida" and "valida" are different names, as are
  "Peña" and "Pena".
- TESTING_PREFIX: every testing project's stored name, and every testing
  volume's file name stem and reference code, begin with it; no real
  project's name or directory, and no real volume's stem or reference
  code, does (in_reserved_namespace). A testing project's directory under
  the projects root is project_dir_name of its stored name, so it begins
  with the prefix too, and a real one never does: the space of a directory
  under the projects root is read from its name alone
  (is_testing_directory).
- TESTING_PROJECT_PREFIX: TESTING_PREFIX and one more hyphen; every testing
  project's stored name begins with it, and so does its directory, since
  project_dir_name keeps hyphens and only strips leading dots, and the
  stored name begins with a letter. A volume's stem never holds two
  hyphens in a row (app.models.collection.FILENAME_STEM_PATTERN), so no
  stem begins with this prefix: testing project directories and testing
  volume stems (app.core.testing_space.series_stem, TESTING_PREFIX and
  digits) are two disjoint series, and a name the testing account chooses
  never takes a directory a stem needs, nor the reverse.
- display_project_name: the name a caller reads and sends for a project,
  the stored name without the project prefix.

Version: 0.3.0
"""

import unicodedata

TESTING_PREFIX = "captua-testing-"
TESTING_PROJECT_PREFIX = TESTING_PREFIX + "-"


def stored_name(name: str) -> str:
    """The name as a project is stored under: stripped, in NFC."""
    return unicodedata.normalize("NFC", name.strip())


def name_key(name: str) -> str:
    """What two project names are compared by: stripped, NFC, case-folded."""
    return stored_name(name).casefold()


def in_reserved_namespace(value) -> bool:
    """Whether `value` (a project name, a stem or a reference code) lies in the testing space's namespace.

    True when it begins with TESTING_PREFIX, or when its directory name
    (app.core.paths.project_dir_name, which folds accents and capitals and
    drops leading dots) does: "Captua-Testing-x" and "cáptua-testing-x"
    are in it too.
    """
    from app.core.paths import project_dir_name

    if not isinstance(value, str):
        return False
    return value.startswith(TESTING_PREFIX) or project_dir_name(value).startswith(TESTING_PREFIX)


def is_testing_directory(directory_name) -> bool:
    """Whether a directory directly under the projects root is the testing space's: its name begins with the prefix."""
    return isinstance(directory_name, str) and directory_name.startswith(TESTING_PREFIX)


def display_project_name(name):
    """The name a caller reads for a project stored under `name`: without the project prefix when it has one.

    A real project's name never begins with TESTING_PREFIX, so the stored
    name alone tells which form to give.
    """
    if isinstance(name, str) and name.startswith(TESTING_PROJECT_PREFIX):
        return name[len(TESTING_PROJECT_PREFIX):]
    return name


# A project's directory under the projects root (app.core.paths.project_dir_name)
# is the project's alone, in both spaces: app.core.storage_ops.refuse_taken_project_directory.
PROJECT_DIRECTORY_TAKEN = "project_directory_taken"
PROJECT_DIRECTORY_TAKEN_MESSAGE = (
    "Another project's files are kept in the directory this name would use. Choose a name that differs "
    "by more than capital letters, accents or punctuation."
)

# A name whose directory name is empty would put the project's files in the projects root itself.
PROJECT_NAME_INVALID = "project_name_invalid"
PROJECT_NAME_INVALID_MESSAGE = (
    "This name gives no directory name: it holds no letter, digit, dot, hyphen or underscore that "
    "Captua can use for the project's directory."
)


class ProjectDirectoryTaken(Exception):
    """A project name refused because its directory is another project's, or already exists on disk: 409."""

    status = 409
    code = PROJECT_DIRECTORY_TAKEN
    message = PROJECT_DIRECTORY_TAKEN_MESSAGE

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message}


class ProjectNameInvalid(ProjectDirectoryTaken):
    """A project name refused because its directory name is empty: 422 project_name_invalid.

    A ProjectDirectoryTaken, so every caller that answers a refused
    directory answers this one with its own status and code.
    """

    status = 422
    code = PROJECT_NAME_INVALID
    message = PROJECT_NAME_INVALID_MESSAGE


def refuse_empty_directory(name: str) -> None:
    """ProjectNameInvalid when project_dir_name(name) is empty, which would make the projects root the project's directory."""
    from app.core.paths import project_dir_name

    if not project_dir_name(name):
        raise ProjectNameInvalid()
