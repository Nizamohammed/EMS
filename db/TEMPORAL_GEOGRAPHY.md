# Temporal Geography & Constituency Lineage — Design Spec (deferred implementation)

> Status: **DESIGN ONLY.** The forward-compatible hook (`delimitation_era` + `roll.era_id`)
> is already in `db/schema.sql`. The full model below is implemented when we first
> ingest rolls published under a *different* delimitation than existing rows. Every roll
> we have today (2026 SIR) uses the **2008 Delimitation**, so nothing here is exercised yet.

## 1. The problem

The geography spine (`state`, `district`, `parliamentary_constituency`,
`assembly_constituency`, `part`, `section`) is keyed on **codes and numbers with no time
dimension**:

- `state (state_cd)` unique
- `assembly_constituency (state_id, ac_no)` unique
- `part (ac_id, part_no)` unique

Real-world geography is **not** static. The same physical area's `(ac_no, part_no)`
identity changes across four kinds of event:

| Event | Example | Effect on keys |
|---|---|---|
| **Renumber** | AC 45 → AC 47, no boundary change | `ac_no` collides with a *different* future area |
| **Split** | AC 45 → AC 45 + AC 46 | one predecessor, many successors |
| **Merge** | AC 45 + AC 46 → AC 45 | many predecessors, one successor |
| **Delimitation** | boundaries redrawn (~decadal, next after the 2026 census) | wholesale re-map of `ac_no`/`part_no` |
| **Bifurcation** | Andhra Pradesh → AP (S01) + Telangana (S29), 2014 | new `state_cd`; ACs re-homed |

Without a temporal model, ingesting a post-change roll **silently conflates** "part 12,
era A" with "part 12, era B" on the unique key — corrupting per-part history and any
person timeline that crosses the boundary.

## 2. The model

### 2.1 Era (already in schema)

```sql
create table delimitation_era (
    era_id bigint generated always as identity primary key,
    era_name text not null unique,      -- '2008 Delimitation', '2027 Delimitation', ...
    effective_from date, effective_to date
);
```

`roll.era_id` (already added) stamps each roll's boundary regime. **This is the load-bearing
hedge**: it lets every existing row be assigned to an era retroactively, so the migration
below never has to guess.

### 2.2 Era-scope the geography keys

Change the uniqueness of the mutable-numbering entities from global to **per-era**:

```sql
alter table assembly_constituency add column era_id bigint references delimitation_era(era_id);
alter table part                  add column era_id bigint references delimitation_era(era_id);

-- replace the old unique constraints:
--   assembly_constituency (state_id, ac_no)      -> (state_id, era_id, ac_no)
--   part                  (ac_id, part_no)        -> (ac_id, era_id, part_no)
```

`state`, `district`, and `parliamentary_constituency` may or may not need era-scoping
depending on the change (bifurcation touches `state`; a normal delimitation does not).
Keep `state_cd` era-independent and model bifurcation via lineage (2.3) instead.

`roll` then references the era-correct `part` row. A part that is unchanged across eras is
represented as **two rows** (one per era) linked by lineage — this is deliberate: the
numbering *identity* differs even when the ground doesn't, and lineage records the sameness.

### 2.3 Lineage graph (predecessor → successor)

A directed edge set capturing how areas map across an era boundary:

```sql
create table ac_lineage (
    ac_lineage_id     bigint generated always as identity primary key,
    predecessor_ac_id bigint references assembly_constituency(ac_id),
    successor_ac_id   bigint references assembly_constituency(ac_id),
    change_type       text not null check (change_type in
                        ('unchanged','renumber','split','merge','new','abolished','rehomed')),
    effective_era_id  bigint references delimitation_era(era_id),
    notes             text
);
-- (a parallel part_lineage for polling-station-level continuity, same shape)
```

- **renumber / unchanged**: 1 predecessor → 1 successor
- **split**: 1 predecessor → N successors (N edges)
- **merge**: N predecessors → 1 successor (N edges)
- **new**: 0 predecessors → 1 successor (predecessor NULL)
- **abolished**: 1 predecessor → 0 successors (successor NULL)
- **rehomed** (bifurcation): successor lives under a new `state`

### 2.4 Physical polling-station identity (optional, strongest form)

The most robust variant separates a **stable physical station** from its era-scoped
numbering. A `polling_station` gets a durable surrogate identity (anchored on
location/lat-lon/address), and `(ac_no, part_no)` become era-scoped *attributes* of it via
`part`. This makes "trace this building across every delimitation" a direct FK walk instead
of a lineage traversal. Heavier; adopt only if station-level longitudinal analysis is a
first-class requirement.

## 3. Migration path (when triggered)

1. Backfill `era_id` on all existing `assembly_constituency` / `part` rows to the
   `'2008 Delimitation'` era (all current data). `roll.era_id` is already set by the loader.
2. Add the `era_id` columns + swap the unique constraints (per 2.2).
3. Create `ac_lineage` / `part_lineage`, seed `unchanged` edges for the existing era as the
   base case.
4. On first ingest of a new-era roll: create new era-scoped `ac`/`part` rows, and record the
   lineage edges (sourced from the ECI/Delimitation Commission notification — this mapping
   is published, not inferred).
5. Person timelines automatically survive: `person` / `placement` key on the elector
   snapshot, not geography, so a person who is `shifted_out` of a pre-era part and re-added
   in a post-era part is still one person (same EPIC via `person_epic`, or Tier-2 fuzzy).

## 4. Query examples (post-implementation)

```sql
-- Every era-identity of the area currently known as (state S11, AC 47):
with recursive lineage(ac_id) as (
    select ac_id from assembly_constituency ac
      join state s on s.state_id = ac.state_id
     where s.state_cd='S11' and ac.ac_no=47
  union
    select l.predecessor_ac_id from ac_lineage l join lineage on l.successor_ac_id = lineage.ac_id
)
select * from lineage;

-- All rolls for a physical area across every era (via lineage), newest first:
--   walk ac_lineage to gather the ac_id set, then join part -> roll, order by date.
```

## 5. Why deferred

- **Not exercised yet**: 100% of current rolls are one era (2008 Delimitation). There is no
  second era to validate against, so building it now means shipping unexercised schema.
- **Lineage is authoritative, not inferred**: split/merge/renumber mappings come from the
  Delimitation Commission's published notification. We implement when we have both a
  second-era roll *and* its official lineage table — otherwise we'd be guessing edges.
- **The hedge is already in place**: because `roll.era_id` is stamped from day one, no
  existing data has to be re-derived or disambiguated when we do implement — the migration
  above is purely additive.
