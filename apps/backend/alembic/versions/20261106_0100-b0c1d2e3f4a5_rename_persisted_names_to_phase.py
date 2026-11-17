"""rename persisted names from "stage" to "phase" (agentic-only-flow-redesign task 47.7)

Chained onto ``a9b0c1d2e3f4`` (``add_technique_commands_input_arguments``), the
previous head. The code now calls every pipeline step a *phase* by its name
(``operationalization``, ``flow``, ``environment``, ``execution``,
``evaluation``) instead of a numbered "stage"; this migration brings the
persisted names and the one numeric column in line with the ORM. No product
behavior changes.

What ``upgrade`` changes
------------------------
* Tables: ``stage_runs`` -> ``phase_runs``, ``agent_stage_outputs`` ->
  ``agent_phase_outputs``, ``agent_stage_traces`` -> ``agent_phase_traces``.
* Columns: ``agent_iterations.refined_stage`` -> ``refined_phase``,
  ``agent_phase_outputs.stage`` -> ``phase``, ``experience_memory.stage`` ->
  ``phase``, ``agent_phase_traces.stage_output_id`` -> ``phase_output_id``.
* ``stage_runs.stage`` (INTEGER 1..6, the six-stage numbering of
  ``e907e49a84c5``) becomes ``phase_runs.phase`` (VARCHAR NOT NULL) plus
  ``phase_runs.subphase`` (VARCHAR NULL): 1 -> ``input``/``stix_package``,
  2 -> ``input``/``campaign``, 3 -> ``flow``, 4 -> ``operationalization``,
  5 -> ``environment``, 6 -> ``execution``. ``subphase`` keeps rows 1 and 2
  apart so ``downgrade`` is exact.
* The four named CHECK constraints that mention the renamed columns are dropped
  and recreated with the new names (portable on both databases).
* Explicit and database-generated constraint, index and sequence names that
  contain ``stage`` or ``estagio`` are renamed. On PostgreSQL they are found by
  reflection (``pg_constraint`` / ``pg_class``) because the exact generated
  names depend on each database's history (for example
  ``execucoes_estagio_pkey``). On SQLite only named foreign keys and UNIQUE
  constraints exist, and they are recreated by batch mode.
* JSON key ``substages`` -> ``subphases`` (first level only) in
  ``agent_phase_outputs.payload`` and ``agent_phase_traces.processing``.

Safety
------
Before any change the migration checks that no destination name exists, that
every ``stage_runs.stage`` is in 1..6 and that no JSON document holds both keys;
any conflict raises ``RuntimeError`` naming the offender. PostgreSQL runs the
migration in one transaction (transactional DDL), so a failure leaves nothing
changed; SQLite batches are atomic and the checks run before the first one.

Symmetry
--------
``downgrade`` is the exact inverse in the opposite order. It aborts, naming the
value, when a ``phase``/``subphase`` pair has no number in the inverse of
``_NUMBER_TO_PHASE``. Database-generated names go back to the canonical form a
``stage_runs`` table created today would get (``stage_runs_pkey``,
``stage_runs_id_seq``); the older ``execucoes_estagio_*`` names were never
referenced by any migration or by code and are not restored. Explicit names
(``fk_stage_runs_chosen_flow_id_attack_flows``,
``uq_agent_stage_traces_stage_output_id`` and so on) are restored exactly. On
PostgreSQL the recreated ``stage_runs.stage`` column is appended last, so the
column order can differ from the original.

The migration does not import ``app.models``: enum values are copied as
literals, as in the earlier migrations.

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-11-06 01:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b0c1d2e3f4a5"
down_revision: Union[str, None] = "a9b0c1d2e3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (old table, new table)
_TABLES: tuple[tuple[str, str], ...] = (
    ("stage_runs", "phase_runs"),
    ("agent_stage_outputs", "agent_phase_outputs"),
    ("agent_stage_traces", "agent_phase_traces"),
)
_NEW_TO_OLD_TABLE: dict[str, str] = {new: old for old, new in _TABLES}

# (table after the rename, old column, new column)
_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("agent_iterations", "refined_stage", "refined_phase"),
    ("agent_phase_outputs", "stage", "phase"),
    ("experience_memory", "stage", "phase"),
    ("agent_phase_traces", "stage_output_id", "phase_output_id"),
)

# Legacy six-stage number -> (phase, subphase).
_NUMBER_TO_PHASE: dict[int, tuple[str, str | None]] = {
    1: ("input", "stix_package"),
    2: ("input", "campaign"),
    3: ("flow", None),
    4: ("operationalization", None),
    5: ("environment", None),
    6: ("execution", None),
}
_PHASE_TO_NUMBER: dict[tuple[str, str | None], int] = {
    pair: number for number, pair in _NUMBER_TO_PHASE.items()
}

# (table after the rename, column, old key, new key)
_JSON_KEYS: tuple[tuple[str, str, str, str], ...] = (
    ("agent_phase_outputs", "payload", "substages", "subphases"),
    ("agent_phase_traces", "processing", "substages", "subphases"),
)

# Ordered substring rewrites for explicit and database-generated names.
_NAME_REWRITES: tuple[tuple[str, str], ...] = (
    ("execucoes_estagio", "phase_runs"),
    ("stage_runs", "phase_runs"),
    ("agent_stage_", "agent_phase_"),
    ("stage_output_id", "phase_output_id"),
    ("refined_stage", "refined_phase"),
    ("estagio", "phase"),
    ("stage", "phase"),
)
# Inverse rewrites for ``downgrade`` (canonical names, see the module docstring).
_REVERSE_NAME_REWRITES: tuple[tuple[str, str], ...] = (
    ("phase_runs", "stage_runs"),
    ("agent_phase_", "agent_stage_"),
    ("phase_output_id", "stage_output_id"),
    ("refined_phase", "refined_stage"),
    ("phase", "stage"),
)
_UPGRADE_NAME_PATTERN = "stage|estagio"
_DOWNGRADE_NAME_PATTERN = "phase"

# Tables whose constraints and indexes are scanned for names to rewrite.
_NAME_SCAN_TABLES_AFTER: tuple[str, ...] = (
    "phase_runs",
    "agent_phase_outputs",
    "agent_phase_traces",
    "agent_iterations",
    "experience_memory",
)

# AgentPhase and TranslationSource value sets as installed by ``e1f2a3b4c5d6``
# and ``b2c3d4e5f6a7`` (literals, no import of ``app.models``).
_AGENT_PHASE_VALUES: tuple[str, ...] = (
    "operationalization",
    "flow",
    "environment",
    "execution",
    "evaluation",
)
_ORIGIN_VALUES: tuple[str, ...] = ("human_curation", "ai_generated")

# (table before, column before, check before, table after, column after,
#  check after, allowed values)
_CHECKS: tuple[tuple[str, str, str, str, str, str, tuple[str, ...]], ...] = (
    (
        "agent_iterations",
        "refined_stage",
        "ck_agent_iterations_refined_stage",
        "agent_iterations",
        "refined_phase",
        "ck_agent_iterations_refined_phase",
        _AGENT_PHASE_VALUES,
    ),
    (
        "agent_stage_outputs",
        "stage",
        "ck_agent_stage_outputs_stage",
        "agent_phase_outputs",
        "phase",
        "ck_agent_phase_outputs_phase",
        _AGENT_PHASE_VALUES,
    ),
    (
        "agent_stage_outputs",
        "origin",
        "ck_agent_stage_outputs_origin",
        "agent_phase_outputs",
        "origin",
        "ck_agent_phase_outputs_origin",
        _ORIGIN_VALUES,
    ),
    (
        "experience_memory",
        "stage",
        "ck_experience_memory_stage",
        "experience_memory",
        "phase",
        "ck_experience_memory_phase",
        _AGENT_PHASE_VALUES,
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _rewrite(name: str, rules: Sequence[tuple[str, str]]) -> str:
    """Apply the ordered substring ``rules`` to ``name``."""
    for old, new in rules:
        name = name.replace(old, new)
    return name


def _quote(name: str) -> str:
    return op.get_bind().dialect.identifier_preparer.quote(name)


def _check_expr(column: str, values: Sequence[str]) -> str:
    """Render ``column IN ('v1', ...)`` (fixed internal literals, no user input)."""
    in_list = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({in_list})"


def _columns_of(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _check_names(table: str) -> set[str]:
    return {
        c["name"] for c in sa.inspect(op.get_bind()).get_check_constraints(table) if c.get("name")
    }


def _case_sql(column: str, mapping: dict[Any, str | None]) -> str:
    """``CASE column WHEN k THEN 'v' ... END`` from module constants (no user input)."""
    whens = " ".join(
        f"WHEN {key!r} THEN {'NULL' if value is None else repr(value)}"
        for key, value in mapping.items()
    )
    return f"CASE {column} {whens} END"


# -- JSON keys ---------------------------------------------------------------
def _json_rows(table: str, column: str) -> tuple[Any, list[Any]]:
    """Return a typed lightweight table and its ``(id, value)`` rows."""
    bind = op.get_bind()
    col_type = next(c["type"] for c in sa.inspect(bind).get_columns(table) if c["name"] == column)
    handle = sa.table(table, sa.column("id", sa.Integer), sa.column(column, col_type))
    rows = bind.execute(sa.select(handle.c.id, handle.c[column])).all()
    return handle, list(rows)


def _swap_key(value: Any, old: str, new: str) -> tuple[bool, Any]:
    """Rename the first-level key ``old`` to ``new``, keeping key order."""
    if not isinstance(value, dict) or old not in value:
        return False, value
    return True, {(new if key == old else key): item for key, item in value.items()}


def _check_json_conflicts(table_of: Callable[[str], str], reverse: bool) -> None:
    for table_after, column, old_key, new_key in _JSON_KEYS:
        src, dst = (new_key, old_key) if reverse else (old_key, new_key)
        _handle, rows = _json_rows(table_of(table_after), column)
        for row_id, value in rows:
            if isinstance(value, dict) and src in value and dst in value:
                raise RuntimeError(
                    f"Migration aborted: {table_of(table_after)}.{column} row id={row_id} "
                    f"already holds both '{src}' and '{dst}' keys."
                )


def _convert_json(reverse: bool) -> None:
    bind = op.get_bind()
    for table_after, column, old_key, new_key in _JSON_KEYS:
        src, dst = (new_key, old_key) if reverse else (old_key, new_key)
        handle, rows = _json_rows(table_after, column)
        for row_id, value in rows:
            changed, converted = _swap_key(value, src, dst)
            if changed:
                bind.execute(
                    sa.update(handle).where(handle.c.id == row_id).values({column: converted})
                )


# -- PostgreSQL names by reflection -----------------------------------------
def _pg_name_plan(
    tables: Sequence[str], pattern: str, rules: Sequence[tuple[str, str]]
) -> list[tuple[str, str | None, str, str]]:
    """Plan renames as ``(kind, table, old, new)``; kind is constraint/index/sequence.

    Check constraints are excluded: the named ones are dropped and recreated
    explicitly, and ``_check_unknown_checks`` rejects any other that matches.
    """
    bind = op.get_bind()
    plan: list[tuple[str, str | None, str, str]] = []
    params = {"pattern": pattern, "tables": list(tables)}
    constraints = bind.execute(
        sa.text(
            "SELECT t.relname, c.conname FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = current_schema() AND c.contype <> 'c' "
            "AND t.relname = ANY(:tables) AND c.conname ~ :pattern "
            "ORDER BY t.relname, c.conname"
        ).bindparams(sa.bindparam("tables", type_=sa.ARRAY(sa.String()))),
        params,
    ).all()
    for table, name in constraints:
        plan.append(("constraint", table, name, _rewrite(name, rules)))
    indexes = bind.execute(
        sa.text(
            "SELECT t.relname, i.relname FROM pg_index x "
            "JOIN pg_class i ON i.oid = x.indexrelid "
            "JOIN pg_class t ON t.oid = x.indrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = current_schema() AND t.relname = ANY(:tables) "
            "AND i.relname ~ :pattern ORDER BY i.relname"
        ).bindparams(sa.bindparam("tables", type_=sa.ARRAY(sa.String()))),
        params,
    ).all()
    for table, name in indexes:
        plan.append(("index", table, name, _rewrite(name, rules)))
    sequences = bind.execute(
        sa.text(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relkind = 'S' "
            "AND c.relname ~ :pattern ORDER BY c.relname"
        ),
        {"pattern": pattern},
    ).all()
    for (name,) in sequences:
        plan.append(("sequence", None, name, _rewrite(name, rules)))
    return [item for item in plan if item[2] != item[3]]


def _pg_check_name_collisions(plan: Sequence[tuple[str, str | None, str, str]]) -> None:
    """Abort when a planned new name already exists (relations share one namespace)."""
    bind = op.get_bind()
    relnames = {
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema()"
            )
        )
    }
    constraints = {
        (row[0], row[1])
        for row in bind.execute(
            sa.text(
                "SELECT t.relname, c.conname FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = current_schema()"
            )
        )
    }
    for kind, table, old, new in plan:
        taken = new in relnames
        if kind == "constraint" and table is not None:
            taken = (table, new) in constraints or taken
        if taken:
            raise RuntimeError(
                f"Migration aborted: cannot rename {kind} '{old}' to '{new}'; "
                "the destination name already exists."
            )


def _pg_apply_names(tables: Sequence[str], pattern: str, rules: Sequence[tuple[str, str]]) -> None:
    """Rename constraints, then indexes, then sequences (constraint renames move their index)."""
    bind = op.get_bind()
    for kind, table, old, new in _pg_name_plan(tables, pattern, rules):
        if kind != "constraint":
            continue
        bind.execute(
            sa.text(f"ALTER TABLE {_quote(table)} RENAME CONSTRAINT {_quote(old)} TO {_quote(new)}")
        )
    for kind, _table, old, new in _pg_name_plan(tables, pattern, rules):
        if kind == "index":
            bind.execute(sa.text(f"ALTER INDEX {_quote(old)} RENAME TO {_quote(new)}"))
        elif kind == "sequence":
            bind.execute(sa.text(f"ALTER SEQUENCE {_quote(old)} RENAME TO {_quote(new)}"))


# -- SQLite names (named FKs and UNIQUE only) --------------------------------
def _sqlite_apply_names(tables: Sequence[str], rules: Sequence[tuple[str, str]]) -> None:
    for table in tables:
        insp = sa.inspect(op.get_bind())
        fks = [
            fk
            for fk in insp.get_foreign_keys(table)
            if fk.get("name") and _rewrite(fk["name"], rules) != fk["name"]
        ]
        uqs = [
            uq
            for uq in insp.get_unique_constraints(table)
            if uq.get("name") and _rewrite(uq["name"], rules) != uq["name"]
        ]
        if not fks and not uqs:
            continue
        with op.batch_alter_table(table) as batch_op:
            for fk in fks:
                batch_op.drop_constraint(fk["name"], type_="foreignkey")
            for uq in uqs:
                batch_op.drop_constraint(uq["name"], type_="unique")
        with op.batch_alter_table(table) as batch_op:
            for fk in fks:
                batch_op.create_foreign_key(
                    _rewrite(fk["name"], rules),
                    fk["referred_table"],
                    fk["constrained_columns"],
                    fk["referred_columns"],
                )
            for uq in uqs:
                batch_op.create_unique_constraint(_rewrite(uq["name"], rules), uq["column_names"])


# -- Pre-checks --------------------------------------------------------------
def _check_unknown_checks(
    tables: Sequence[str], pattern_words: Sequence[str], known: set[str]
) -> None:
    """Reject a CHECK we would neither drop nor recreate that mentions the renamed words."""
    for table in tables:
        for name in _check_names(table):
            if name not in known and any(word in name for word in pattern_words):
                raise RuntimeError(
                    f"Migration aborted: unexpected CHECK constraint '{name}' on '{table}'."
                )


def _precheck_upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for old, new in _TABLES:
        if old not in existing:
            raise RuntimeError(f"Migration aborted: source table '{old}' does not exist.")
        if new in existing:
            raise RuntimeError(f"Migration aborted: destination table '{new}' already exists.")
    for table_after, old_col, new_col in _COLUMNS:
        cols = _columns_of(_NEW_TO_OLD_TABLE.get(table_after, table_after))
        if old_col not in cols:
            raise RuntimeError(
                f"Migration aborted: column '{table_after}.{old_col}' does not exist."
            )
        if new_col in cols:
            raise RuntimeError(
                f"Migration aborted: destination column '{table_after}.{new_col}' already exists."
            )
    run_cols = _columns_of("stage_runs")
    for new_col in ("phase", "subphase"):
        if new_col in run_cols:
            raise RuntimeError(
                f"Migration aborted: destination column 'stage_runs.{new_col}' already exists."
            )
    # Every legacy number must be in 1..6.
    bad = [
        row[0]
        for row in bind.execute(
            sa.text("SELECT DISTINCT stage FROM stage_runs WHERE stage NOT BETWEEN 1 AND 6")
        )
    ]
    if bad:
        raise RuntimeError(
            f"Migration aborted: stage_runs.stage holds values outside 1..6: {sorted(bad)!r}."
        )
    _check_json_conflicts(lambda t: _NEW_TO_OLD_TABLE.get(t, t), reverse=False)
    known = {before for _t, _c, before, *_rest in _CHECKS}
    _check_unknown_checks(
        [_NEW_TO_OLD_TABLE.get(t, t) for t in _NAME_SCAN_TABLES_AFTER],
        ("stage", "estagio"),
        known,
    )
    if _is_postgres():
        old_tables = [_NEW_TO_OLD_TABLE.get(t, t) for t in _NAME_SCAN_TABLES_AFTER]
        _pg_check_name_collisions(_pg_name_plan(old_tables, _UPGRADE_NAME_PATTERN, _NAME_REWRITES))


def _precheck_downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for old, new in _TABLES:
        if new not in existing:
            raise RuntimeError(f"Migration aborted: table '{new}' does not exist.")
        if old in existing:
            raise RuntimeError(f"Migration aborted: destination table '{old}' already exists.")
    for table_after, old_col, new_col in _COLUMNS:
        cols = _columns_of(table_after)
        if new_col not in cols:
            raise RuntimeError(
                f"Migration aborted: column '{table_after}.{new_col}' does not exist."
            )
        if old_col in cols:
            raise RuntimeError(
                f"Migration aborted: destination column '{table_after}.{old_col}' already exists."
            )
    if "stage" in _columns_of("phase_runs"):
        raise RuntimeError(
            "Migration aborted: destination column 'phase_runs.stage' already exists."
        )
    for pair in bind.execute(sa.text("SELECT DISTINCT phase, subphase FROM phase_runs")):
        key = (pair[0], pair[1])
        if key not in _PHASE_TO_NUMBER:
            raise RuntimeError(
                f"Migration aborted: phase_runs has phase={key[0]!r}, subphase={key[1]!r}, "
                "which has no legacy stage number."
            )
    _check_json_conflicts(lambda t: t, reverse=True)
    known = {after for _t, _c, _b, _ta, _ca, after, _v in _CHECKS}
    _check_unknown_checks(_NAME_SCAN_TABLES_AFTER, ("phase",), known)
    if _is_postgres():
        _pg_check_name_collisions(
            _pg_name_plan(_NAME_SCAN_TABLES_AFTER, _DOWNGRADE_NAME_PATTERN, _REVERSE_NAME_REWRITES)
        )


# -- CHECK drop / recreate ---------------------------------------------------
def _drop_checks(use_after: bool) -> list[int]:
    """Drop the known named CHECKs that exist; return their indexes in ``_CHECKS``."""
    dropped: list[int] = []
    for index, (t_before, _cb, n_before, t_after, _ca, n_after, _values) in enumerate(_CHECKS):
        table, name = (t_after, n_after) if use_after else (t_before, n_before)
        if name not in _check_names(table):
            continue
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_constraint(name, type_="check")
        dropped.append(index)
    return dropped


def _create_checks(indexes: Sequence[int], use_after: bool) -> None:
    """Recreate the CHECKs at ``indexes`` with the after (new) or before (old) naming."""
    for index in indexes:
        t_before, c_before, n_before, t_after, c_after, n_after, values = _CHECKS[index]
        table, column, name = (
            (t_after, c_after, n_after) if use_after else (t_before, c_before, n_before)
        )
        with op.batch_alter_table(table) as batch_op:
            batch_op.create_check_constraint(name, _check_expr(column, values))


# ---------------------------------------------------------------------------
# upgrade / downgrade
# ---------------------------------------------------------------------------
def upgrade() -> None:
    bind = op.get_bind()

    # (1) Pre-check everything before the first change.
    _precheck_upgrade()

    # (2) CHECKs that mention the renamed columns.
    recreate = _drop_checks(use_after=False)

    # (3) Tables.
    for old, new in _TABLES:
        op.rename_table(old, new)

    # (4) Columns.
    for table_after, old_col, new_col in _COLUMNS:
        with op.batch_alter_table(table_after) as batch_op:
            batch_op.alter_column(old_col, new_column_name=new_col)

    # (5) stage INTEGER -> phase + subphase.
    with op.batch_alter_table("phase_runs") as batch_op:
        batch_op.add_column(sa.Column("phase", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("subphase", sa.String(), nullable=True))
    bind.execute(
        sa.text(
            "UPDATE phase_runs SET "  # noqa: S608 -- CASE built from module constants only
            f"phase = {_case_sql('stage', {n: p for n, (p, _s) in _NUMBER_TO_PHASE.items()})}, "
            f"subphase = {_case_sql('stage', {n: s for n, (_p, s) in _NUMBER_TO_PHASE.items()})}"
        )
    )
    with op.batch_alter_table("phase_runs") as batch_op:
        batch_op.alter_column("phase", existing_type=sa.String(), nullable=False)
        batch_op.drop_column("stage")

    # (6) CHECKs recreated with the new names and columns.
    _create_checks(recreate, use_after=True)

    # (7) Explicit and generated names.
    if _is_postgres():
        _pg_apply_names(_NAME_SCAN_TABLES_AFTER, _UPGRADE_NAME_PATTERN, _NAME_REWRITES)
    else:
        _sqlite_apply_names(_NAME_SCAN_TABLES_AFTER, _NAME_REWRITES)

    # (8) JSON keys.
    _convert_json(reverse=False)


def downgrade() -> None:
    bind = op.get_bind()

    # (1') Pre-check everything before the first change.
    _precheck_downgrade()

    # (8') JSON keys.
    _convert_json(reverse=True)

    # (7') Names back to the canonical ``stage`` form, then drop the new CHECKs.
    if _is_postgres():
        _pg_apply_names(_NAME_SCAN_TABLES_AFTER, _DOWNGRADE_NAME_PATTERN, _REVERSE_NAME_REWRITES)
    else:
        _sqlite_apply_names(_NAME_SCAN_TABLES_AFTER, _REVERSE_NAME_REWRITES)
    recreate = _drop_checks(use_after=True)

    # (5') phase + subphase -> stage INTEGER.
    with op.batch_alter_table("phase_runs") as batch_op:
        batch_op.add_column(sa.Column("stage", sa.Integer(), nullable=True))
    whens = " ".join(
        f"WHEN phase = '{phase}' AND subphase {'IS NULL' if sub is None else f'= {sub!r}'} THEN {number}"
        for (phase, sub), number in _PHASE_TO_NUMBER.items()
    )
    bind.execute(sa.text(f"UPDATE phase_runs SET stage = CASE {whens} END"))  # noqa: S608 -- CASE built from module constants only
    with op.batch_alter_table("phase_runs") as batch_op:
        batch_op.alter_column("stage", existing_type=sa.Integer(), nullable=False)
        batch_op.drop_column("phase")
        batch_op.drop_column("subphase")

    # (4') Columns.
    for table_after, old_col, new_col in _COLUMNS:
        with op.batch_alter_table(table_after) as batch_op:
            batch_op.alter_column(new_col, new_column_name=old_col)

    # (3') Tables.
    for old, new in _TABLES:
        op.rename_table(new, old)

    # (2') CHECKs back with the old names and columns.
    _create_checks(recreate, use_after=False)
