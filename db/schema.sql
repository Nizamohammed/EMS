-- ============================================================================
-- EMS — Electoral Management System (India)
-- Core database schema  (PostgreSQL 15+)
-- ----------------------------------------------------------------------------
-- Design summary (see CLAUDE.md and the session design discussion):
--   * 4 layers: geography spine -> roll snapshot -> QC/checksum -> derived identity.
--   * The DB stores STRUCTURED data only. The actual PDFs live in an object
--     store; `roll` holds a content hash + URIs that point at them.
--   * Each published PDF is a faithful, versioned SNAPSHOT (`roll` + `elector`).
--     History is preserved by keeping every roll's rows, never overwriting.
--   * The page-12 summary + page-1 cover counts are stored as a CHECKSUM and
--     reconciled against extracted electors (see v_roll_reconciliation).
--   * Identity (`person`/`placement`) is DERIVED. Tier 1 (EPIC-keyed) is built
--     now; Tier 2 (fuzzy cross-roll matching) is stubbed via match_method.
--   * Map geometry is a deferred extension point: lat/lon columns exist now;
--     polygon geometry arrives later via a PostGIS migration (see end of file).
--
-- Requires PostgreSQL 15+ (uses CREATE UNIQUE INDEX ... NULLS NOT DISTINCT).
-- Conventions: snake_case; surrogate bigint identity PKs; natural keys enforced
-- with UNIQUE constraints; small fixed enumerations via CHECK; reference data
-- with descriptions (deletion reasons) via a lookup table.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Reference / lookup
-- ----------------------------------------------------------------------------

-- Deletion reason codes printed in the roll's deletion entries (legend on p12).
-- A lookup table (not a CHECK) because the meaning is genuine reference data.
create table deletion_reason (
    code         char(1) primary key check (code in ('E','S','R','M','Q')),
    description  text not null
);

insert into deletion_reason (code, description) values
    ('E', 'Expired'),
    ('S', 'Shifted / Change of Residence'),
    ('R', 'Duplicate'),
    ('M', 'Missing'),
    ('Q', 'Disqualified');

-- ----------------------------------------------------------------------------
-- Layer 1 — Geography spine (reference data shared by every roll)
-- Sourced primarily from the ECI open API; enriched/confirmed from the cover.
-- ----------------------------------------------------------------------------

create table state (
    state_id    bigint generated always as identity primary key,
    state_cd    text not null unique check (state_cd ~ '^[SU][0-9]{2}$'),  -- S01..S29, U01..U08
    state_name  text not null
);

create table district (
    district_id    bigint generated always as identity primary key,
    state_id       bigint not null references state(state_id),
    district_cd    text,                       -- from API (e.g. S0429); may be unknown from PDF alone
    district_name  text not null,
    unique (state_id, district_name)
);

create table parliamentary_constituency (
    pc_id               bigint generated always as identity primary key,
    state_id            bigint not null references state(state_id),
    pc_no               integer not null check (pc_no >= 1),
    pc_name             text not null,
    reservation_status  text check (reservation_status in ('GEN','SC','ST')),
    centroid_lat        numeric(9,6),           -- map hook (point); polygons via PostGIS later
    centroid_lon        numeric(9,6),
    unique (state_id, pc_no)
);

create table assembly_constituency (
    ac_id               bigint generated always as identity primary key,
    state_id            bigint not null references state(state_id),
    district_id         bigint references district(district_id),  -- nullable: an AC can span districts
    pc_id               bigint references parliamentary_constituency(pc_id),  -- AC nests under exactly one PC (N:1)
    ac_no               integer not null check (ac_no >= 1),
    ac_name             text not null,
    reservation_status  text check (reservation_status in ('GEN','SC','ST')),
    centroid_lat        numeric(9,6),
    centroid_lon        numeric(9,6),
    unique (state_id, ac_no)
);

-- A Part = one polling-station unit; the atomic thing a roll PDF is published for.
-- Polling-station attributes are 1:1 with the part.
create table part (
    part_id                   bigint generated always as identity primary key,
    ac_id                     bigint not null references assembly_constituency(ac_id),
    part_no                   integer not null check (part_no >= 1),
    -- area details (cover section 2)
    main_town_or_village      text,
    post_office               text,
    police_station            text,
    block                     text,
    tehsil_mandal             text,
    taluk                     text,            -- kept separate from tehsil_mandal (distinct labels on the roll)
    pin_code                  text check (pin_code ~ '^[0-9]{6}$'),
    -- polling station (cover section 3)
    polling_station_no        integer check (polling_station_no is null or polling_station_no >= 1),
    polling_station_name      text,
    polling_station_address   text,
    polling_station_type      text check (polling_station_type in ('General','Male','Female')),
    num_auxiliary_stations    integer not null default 0 check (num_auxiliary_stations >= 0),
    station_lat               numeric(9,6),    -- map hook (pin); fill from API/geocoding later
    station_lon               numeric(9,6),
    unique (ac_id, part_no)
);

-- A geographic sub-grouping inside a part (e.g. "1-Block 1").
create table section (
    section_id    bigint generated always as identity primary key,
    part_id       bigint not null references part(part_id),
    section_no    integer not null check (section_no >= 1),
    section_name  text,
    unique (part_id, section_no)
);

-- ----------------------------------------------------------------------------
-- Layer 2 — Roll snapshot (faithful, versioned record of each published PDF)
-- ----------------------------------------------------------------------------

create table roll (
    roll_id              bigint generated always as identity primary key,
    part_id              bigint not null references part(part_id),
    year_of_revision     integer not null,
    qualifying_date      date,                 -- "Age as on"
    type_of_revision     text,                 -- e.g. "Special Summary Revision 2026"
    date_of_publication  date,                 -- latest-per-state = max(date_of_publication)
    date_of_updation     date,                 -- footer "Date of Updation" (roll-level / latest)
    roll_identification  text,                 -- verbatim, e.g. "Final Integrated Roll of ..." (mother-roll value)
    roll_type            text not null check (roll_type in ('SIR_FinalRoll','SIR_DraftRoll','Supplement')),
    revision_no          integer,              -- nullable (Draft rolls may lack a Revision token); see uq_roll_natural
    language_code        text not null check (language_code ~ '^[A-Z]{2,3}$'),  -- ENG, HIN, TAM, ...
                                               -- NOTE: normalize on ingest (ENG not EN/English); promote to a
                                               -- language lookup table once the ECI code set is enumerated.
    total_pages          integer check (total_pages is null or total_pages >= 1),
    -- object-store linkage (the DB never stores the blob itself)
    source_pdf_filename  text,
    source_pdf_sha256    text unique,          -- content address: dedup byte-identical re-fetches + integrity
    source_pdf_uri       text,                 -- key/path in the object store
    ocr_payload_uri      text,                 -- compressed OCR-output cache (regenerable; nullable)
    ingested_at          timestamptz not null default now()
);

-- Logical-roll dedup key. NULLS NOT DISTINCT (PG15+) so two Draft rolls with a
-- NULL revision_no that are otherwise identical still collide (prevents
-- duplicate ingestion that would corrupt every per-roll reconciliation count).
-- Pre-15 target: instead make revision_no NOT NULL DEFAULT 0 and use a plain UNIQUE.
create unique index uq_roll_natural
    on roll (part_id, year_of_revision, roll_type, revision_no, language_code) nulls not distinct;

-- Per-roll supplement: the "List of Additions {n} ({from} {to})" section header,
-- and the per-supplement revision identity. One roll can carry several supplements,
-- so this cannot live on `roll` (one row) or `summary_row` (capped, p12-shaped).
create table supplement (
    supplement_id        bigint generated always as identity primary key,
    roll_id              bigint not null references roll(roll_id),
    supplement_no        integer not null check (supplement_no >= 1),
    roll_identification  text,                 -- e.g. "Special Intensive Revision 2026"
    type_of_revision     text,
    additions_from_date  date,                 -- the "(from to)" window on the additions header
    additions_to_date    date,
    date_of_updation     date,                 -- per-supplement, if distinguishable from the roll footer
    unique (roll_id, supplement_no)
);

-- One voter card AS PRINTED in one roll (a roll-scoped appearance, not a person).
create table elector (
    elector_id              bigint generated always as identity primary key,
    roll_id                 bigint not null references roll(roll_id),
    section_id              bigint references section(section_id),  -- see cross-part note below
    serial_no               integer not null check (serial_no >= 1),
    epic_no                 text,              -- nullable (illegible/absent). Expected shape ~ [A-Z]{3}[0-9]{7};
                                               -- NOT regex-enforced (old EPIC formats vary) — see indexes.
    full_name               text not null,
    relation_type           text check (relation_type in ('Father','Husband','Mother','Other')),
    relation_name           text,
    house_number            text,              -- FREE TEXT (numeric, slash-form, or locality) — never assume numeric
    age                     integer check (age is null or age >= 0),
    gender                  text check (gender in ('Male','Female','ThirdGender')),
    photo_present           boolean not null default false,
    entry_status            text not null default 'active'
                                check (entry_status in ('active','deleted','added','modified')),
    deletion_reason_code    char(1) references deletion_reason(code),
    supplement_id           bigint references supplement(supplement_id),  -- which supplement added/modified this entry
    modification_marker_raw text,              -- raw token (#/#1, R/#1, ...) before normalization
    source_page             integer check (source_page is null or source_page >= 1),
    raw_extract             jsonb,             -- raw OCR fields for this card (provenance/debug)
    unique (roll_id, serial_no),               -- serial is a unique position within a roll
    -- a deletion reason only makes sense on a deleted entry
    check (deletion_reason_code is null or entry_status = 'deleted')
    -- DEFERRED GUARD (REL-4): nothing yet forces section.part_id = roll.part_id, so an elector
    -- could (via a loader bug) point at a section of another part. Single per-part writer makes
    -- this low-risk; harden with a composite FK or trigger when ingestion is productionized.
);

-- ----------------------------------------------------------------------------
-- Layer 3 — QC / checksum (the roll's own printed totals)
-- ----------------------------------------------------------------------------

-- Page-12 "Summary of Electors" net block (1:1 with roll).
create table summary (
    summary_id        bigint generated always as identity primary key,
    roll_id           bigint not null unique references roll(roll_id),
    net_male          integer check (net_male is null or net_male >= 0),
    net_female        integer check (net_female is null or net_female >= 0),
    net_third_gender  integer check (net_third_gender is null or net_third_gender >= 0),
    net_total         integer check (net_total is null or net_total >= 0),
    num_modifications integer check (num_modifications is null or num_modifications >= 0)  -- table B; NOT in I+II-III+IV
);

-- The per-roll-type COMPONENT rows of the page-12 table (I Mother Roll, II Additions,
-- III Deletions, IV gender-mod difference). NET is held solely on summary.net_* to
-- avoid two independently-writable copies of the same figures.
-- roll_identification is per-row (Mother Roll vs supplements differ), hence its own column.
create table summary_row (
    summary_row_id       bigint generated always as identity primary key,
    summary_id           bigint not null references summary(summary_id),
    row_ordinal          text not null check (row_ordinal in ('I','II','III','IV')),
    row_label            text,
    roll_type_label      text,                 -- e.g. "Mother Roll", "Supplement 1"
    roll_identification  text,                 -- per-row identification (verbatim from p12)
    supplement_id        bigint references supplement(supplement_id),  -- II/III rows link here; I/IV stay null
    male                 integer,
    female               integer,
    third_gender         integer,
    total                integer,
    unique (summary_id, row_ordinal),
    -- column identity holds for every row, incl. IV (a gender swap still sums)
    check (total is null or male is null or female is null or third_gender is null
           or total = male + female + third_gender),
    -- per-gender counts are non-negative EXCEPT row IV (gender-mod difference can be signed)
    check (row_ordinal = 'IV' or male is null or male >= 0),
    check (row_ordinal = 'IV' or female is null or female >= 0),
    check (row_ordinal = 'IV' or third_gender is null or third_gender >= 0)
);

-- Page-1 cover "Number of electors" mini-table (serial range + net counts; 1:1 with roll).
create table cover_count (
    cover_count_id      bigint generated always as identity primary key,
    roll_id             bigint not null unique references roll(roll_id),
    starting_serial_no  integer check (starting_serial_no is null or starting_serial_no >= 1),
    ending_serial_no    integer,              -- note: (ending - starting + 1) > net_total by #deletions
    net_male            integer check (net_male is null or net_male >= 0),
    net_female          integer check (net_female is null or net_female >= 0),
    net_third_gender    integer check (net_third_gender is null or net_third_gender >= 0),
    net_total           integer check (net_total is null or net_total >= 0),
    check (ending_serial_no is null or starting_serial_no is null or ending_serial_no >= starting_serial_no)
);

-- ----------------------------------------------------------------------------
-- Layer 4 — Derived identity (Tier 1: EPIC-keyed; Tier 2 fuzzy = stub via match_method)
-- ----------------------------------------------------------------------------

-- One actual human, deduplicated across rolls. Tier 1 keys on EPIC.
create table person (
    person_id       bigint generated always as identity primary key,
    epic_no         text not null unique,      -- Tier-1 identity anchor (persons w/o EPIC are a Tier-2 concern)
    canonical_name  text
);

-- Bridge: links one person to one elector appearance => the person's timeline.
-- Kept separate from elector so re-running identity matching never edits the snapshot.
create table placement (
    placement_id      bigint generated always as identity primary key,
    person_id         bigint not null references person(person_id),
    elector_id        bigint not null unique references elector(elector_id),  -- one placement per appearance
    match_method      text not null default 'epic' check (match_method in ('epic','fuzzy','manual')),
    match_confidence  numeric(4,3) check (match_confidence is null or match_confidence between 0 and 1)
                                               -- 1.0 for epic; populated by the Tier-2 matcher later.
                                               -- (Invariant to consider enforcing: epic => confidence = 1.000.)
);

-- ----------------------------------------------------------------------------
-- Indexes (FK columns used in joins + lookup columns)
-- ----------------------------------------------------------------------------
create index ix_district_state              on district(state_id);
create index ix_pc_state                    on parliamentary_constituency(state_id);
create index ix_ac_state                    on assembly_constituency(state_id);
create index ix_ac_district                 on assembly_constituency(district_id);
create index ix_ac_pc                       on assembly_constituency(pc_id);
create index ix_part_ac                     on part(ac_id);
create index ix_section_part                on section(part_id);
create index ix_roll_part                   on roll(part_id);
create index ix_supplement_roll             on supplement(roll_id);
create index ix_elector_roll                on elector(roll_id);
create index ix_elector_section             on elector(section_id);
create index ix_elector_supplement          on elector(supplement_id);
create index ix_elector_epic                on elector(epic_no);
create index ix_summary_row_summary         on summary_row(summary_id);
create index ix_summary_row_supplement      on summary_row(supplement_id);
create index ix_placement_person            on placement(person_id);

-- ----------------------------------------------------------------------------
-- QC helper — reconcile extracted electors against the printed checksums.
-- `reconciles` is TOTAL (never NULL): a missing summary, a cover/summary
-- disagreement, or any per-gender mismatch all surface as reconciles = false.
-- ----------------------------------------------------------------------------
create view v_roll_reconciliation as
select
    r.roll_id,
    count(e.elector_id)                                                              as cards_printed,
    count(*) filter (where e.entry_status = 'deleted')                              as deletions,
    count(*) filter (where e.entry_status = 'added')                                as additions,
    count(*) filter (where e.entry_status = 'modified')                             as modifications,
    count(*) filter (where e.entry_status <> 'deleted')                             as live_cards,
    count(*) filter (where e.entry_status <> 'deleted' and e.gender = 'Male')        as live_male,
    count(*) filter (where e.entry_status <> 'deleted' and e.gender = 'Female')      as live_female,
    count(*) filter (where e.entry_status <> 'deleted' and e.gender = 'ThirdGender') as live_third_gender,
    s.net_total                                                                      as summary_net_total,
    cc.net_total                                                                     as cover_net_total,
    (s.net_total is not null)                                                        as has_summary,
    (cc.net_total = s.net_total)                                                     as cover_matches_summary,
    coalesce(
            (count(*) filter (where e.entry_status <> 'deleted') = s.net_total)
        and (cc.net_total is null or cc.net_total = s.net_total)
        and (s.net_male is null         or count(*) filter (where e.entry_status <> 'deleted' and e.gender = 'Male')        = s.net_male)
        and (s.net_female is null       or count(*) filter (where e.entry_status <> 'deleted' and e.gender = 'Female')      = s.net_female)
        and (s.net_third_gender is null or count(*) filter (where e.entry_status <> 'deleted' and e.gender = 'ThirdGender') = s.net_third_gender),
        false)                                                                       as reconciles
from roll r
left join elector e      on e.roll_id = r.roll_id
left join summary s      on s.roll_id = r.roll_id
left join cover_count cc on cc.roll_id = r.roll_id
group by r.roll_id, s.net_total, s.net_male, s.net_female, s.net_third_gender, cc.net_total;

-- ============================================================================
-- DEFERRED EXTENSION POINTS (not part of v1 core — documented hooks)
-- ----------------------------------------------------------------------------
-- 1. Map geometry (PostGIS):  CREATE EXTENSION postgis;  then add polygon
--    columns + GiST indexes, e.g.
--      ALTER TABLE assembly_constituency ADD COLUMN geom geometry(MultiPolygon,4326);
--      ALTER TABLE parliamentary_constituency ADD COLUMN geom geometry(MultiPolygon,4326);
--    Boundaries are TEMPORAL (delimitation eras) — effective-date them when added.
-- 2. Tier-2 identity (fuzzy):  CREATE EXTENSION pg_trgm;  add trigram indexes on
--    elector(full_name) etc.; persons without EPIC + name/relation/house/age
--    matching populate placement with match_method='fuzzy'. (Relax person.epic_no
--    NOT NULL at that point, e.g. surrogate identity + nullable epic_no.)
-- 3. Native-script transliteration: optional transliterated columns alongside
--    full_name / relation_name for cross-roll matching of non-English rolls.
-- 4. language lookup table mirroring deletion_reason, once the ECI code set is known.
-- ============================================================================
