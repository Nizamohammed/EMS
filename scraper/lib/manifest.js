'use strict';
// Operational manifest store, backed by node:sqlite (built into Node >=22, so
// zero dependencies). Two roles:
//
//   1. Completeness denominator — state / district / ac mirror the roll DB's
//      natural geography keys (state_cd, district_cd, ac_no) so the manifest
//      and db/schema.sql reconcile later. Reference rows are upserted
//      idempotently (re-crawling is safe).
//
//   2. Per-leaf state machine — download_job is the unit of work the control
//      plane drives: one row per (state, ac, year, roll_type, language, part).
//      Status transitions are tracked so we can see exactly where any single
//      leaf stalls. Seeding never clobbers an existing job's status.
//
// Every reference row also keeps the raw API record (json) — if our field
// extraction guesses wrong on a response shape, nothing is lost and we remap.

const { DatabaseSync } = require('node:sqlite');

const SCHEMA = `
create table if not exists state (
  state_cd    text primary key,
  state_name  text not null,
  state_type  text,
  raw         text,
  fetched_at  text
);

create table if not exists district (
  id            integer primary key,
  state_cd      text not null,
  district_cd   text,
  district_name text not null,
  raw           text,
  fetched_at    text,
  unique (state_cd, district_cd)
);

create table if not exists ac (
  id          integer primary key,
  state_cd    text not null,
  ac_no       integer not null,
  ac_name     text,
  ac_id       text,
  district_cd text,
  category    text,            -- reservation: GEN / SC / ST
  raw         text,
  fetched_at  text,
  unique (state_cd, ac_no)
);

-- Per-state crawl health, so a partial crawl is visible and resumable.
create table if not exists crawl_log (
  scope      text primary key,  -- e.g. 'districts:U06', 'constituencies:U06'
  status     text not null,     -- ok | error
  count      integer,
  error      text,
  updated_at text not null
);

-- The available roll menu discovered per AC (years / roll types / languages).
-- Directly answers the §12 disparities (which ACs are English vs native-only,
-- which carry drafts/supplements) and is the source the seeder draws from.
create table if not exists ac_availability (
  state_cd       text not null,
  ac_no          integer not null,
  years_json     text,
  roll_types_json text,
  languages_json text,
  parts_count    integer,
  updated_at     text,
  primary key (state_cd, ac_no)
);

-- The download leaf. Populated by the control plane once parts are enumerated.
create table if not exists download_job (
  id             integer primary key,
  state_cd       text not null,
  district_cd    text,
  ac_no          integer not null,
  year           integer,
  roll_type      text,           -- SIR_FinalRoll | FinalRoll | DraftRoll | Supplement
  language       text,           -- ENG, HIN, TEL, ...
  part_no        integer not null,
  station_name   text,
  status         text not null default 'pending'
                   check (status in ('pending','fetching','fetched','verified','failed','skipped')),
  attempts       integer not null default 0,
  last_error     text,
  expected_count integer,        -- elector count from the part list, for reconciliation
  pdf_sha256     text,
  file_path      text,
  bytes          integer,
  page_count     integer,
  created_at     text,
  updated_at     text,
  unique (state_cd, ac_no, year, roll_type, language, part_no)
);

create index if not exists idx_job_status on download_job(status);
create index if not exists idx_job_state  on download_job(state_cd);
`;

// Columns added after the first download_job shipped; applied idempotently so
// existing manifest.db files migrate forward without a rebuild.
const MIGRATIONS = ['alter table download_job add column station_name text'];

// Pull the first present key from a raw API row (response shapes vary / rotate).
function pick(obj, ...keys) {
  for (const k of keys) {
    if (obj && obj[k] !== undefined && obj[k] !== null && obj[k] !== '') return obj[k];
  }
  return null;
}

class Manifest {
  constructor(dbPath) {
    this.db = new DatabaseSync(dbPath);
    this.db.exec('pragma journal_mode = wal;');
    this.db.exec('pragma foreign_keys = on;');
    this.db.exec(SCHEMA);
    for (const m of MIGRATIONS) {
      try { this.db.exec(m); } catch { /* column already present */ }
    }
    this._now = () => new Date().toISOString();
  }

  close() {
    this.db.close();
  }

  upsertState(row) {
    this.db
      .prepare(
        `insert into state (state_cd, state_name, state_type, raw, fetched_at)
         values (?, ?, ?, ?, ?)
         on conflict(state_cd) do update set
           state_name = excluded.state_name,
           state_type = excluded.state_type,
           raw        = excluded.raw,
           fetched_at = excluded.fetched_at`
      )
      .run(
        pick(row, 'stateCd'),
        pick(row, 'stateName', 'stateNameEn') || '(unknown)',
        pick(row, 'stateType'),
        JSON.stringify(row),
        this._now()
      );
  }

  upsertDistrict(stateCd, row) {
    this.db
      .prepare(
        `insert into district (state_cd, district_cd, district_name, raw, fetched_at)
         values (?, ?, ?, ?, ?)
         on conflict(state_cd, district_cd) do update set
           district_name = excluded.district_name,
           raw           = excluded.raw,
           fetched_at    = excluded.fetched_at`
      )
      .run(
        stateCd,
        pick(row, 'districtCd', 'districtCode'),
        pick(row, 'districtValue', 'districtName', 'districtNameEn') || '(unknown)',
        JSON.stringify(row),
        this._now()
      );
  }

  upsertAc(stateCd, row) {
    this.db
      .prepare(
        `insert into ac (state_cd, ac_no, ac_name, ac_id, district_cd, category, raw, fetched_at)
         values (?, ?, ?, ?, ?, ?, ?, ?)
         on conflict(state_cd, ac_no) do update set
           ac_name     = excluded.ac_name,
           ac_id       = excluded.ac_id,
           district_cd = excluded.district_cd,
           category    = excluded.category,
           raw         = excluded.raw,
           fetched_at  = excluded.fetched_at`
      )
      .run(
        stateCd,
        Number(pick(row, 'asmblyNo', 'acNo', 'acNumber')),
        pick(row, 'asmblyName', 'acName', 'constituencyName'),
        String(pick(row, 'acId', 'asmblyId') ?? ''),
        pick(row, 'districtCd', 'districtCode'),
        pick(row, 'category', 'reservationStatus', 'acType'),
        JSON.stringify(row),
        this._now()
      );
  }

  stateCdByName(name) {
    const r = this.db.prepare('select state_cd from state where lower(state_name) = lower(?)').get(name);
    return r ? r.state_cd : null;
  }

  // Resolve an AC react-select label ("1 - Foo" or "Foo") to its manifest row.
  findAc(stateCd, label) {
    const num = String(label).match(/^(\d+)\s*-\s*/);
    if (num) {
      const r = this.db
        .prepare('select ac_no, district_cd, ac_name from ac where state_cd = ? and ac_no = ?')
        .get(stateCd, Number(num[1]));
      if (r) return r;
    }
    const name = String(label).replace(/^\d+\s*-\s*/, '').trim();
    return (
      this.db
        .prepare('select ac_no, district_cd, ac_name from ac where state_cd = ? and lower(ac_name) = lower(?)')
        .get(stateCd, name) || null
    );
  }

  setAvailability(stateCd, acNo, { years, rollTypes, languages, partsCount }) {
    this.db
      .prepare(
        `insert into ac_availability (state_cd, ac_no, years_json, roll_types_json, languages_json, parts_count, updated_at)
         values (?, ?, ?, ?, ?, ?, ?)
         on conflict(state_cd, ac_no) do update set
           years_json = excluded.years_json, roll_types_json = excluded.roll_types_json,
           languages_json = excluded.languages_json, parts_count = excluded.parts_count,
           updated_at = excluded.updated_at`
      )
      .run(
        stateCd, acNo,
        JSON.stringify(years || []), JSON.stringify(rollTypes || []),
        JSON.stringify(languages || []), partsCount ?? null, this._now()
      );
  }

  // Seed one download leaf. Idempotent: never clobbers an existing job's status
  // (re-running enumeration won't reset progress).
  seedJob(j) {
    const now = this._now();
    const r = this.db
      .prepare(
        `insert into download_job
           (state_cd, district_cd, ac_no, year, roll_type, language, part_no, station_name, status, created_at, updated_at)
         values (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
         on conflict(state_cd, ac_no, year, roll_type, language, part_no) do nothing`
      )
      .run(
        j.stateCd, j.districtCd ?? null, j.acNo, j.year ?? null, j.rollType ?? null,
        j.language ?? null, j.partNo, j.stationName ?? null, now, now
      );
    return r.changes; // 1 if newly inserted, 0 if already existed
  }

  // Identity of a leaf = (state, ac, year, roll_type, language, part). Flip its
  // status and optionally stamp download metadata (sha256, path, bytes, pages).
  markJob(id, status, fields = {}) {
    const sets = ['status = ?', 'updated_at = ?'];
    const vals = [status, this._now()];
    for (const [k, v] of Object.entries(fields)) {
      sets.push(`${k} = ?`);
      vals.push(v);
    }
    vals.push(id.stateCd, id.acNo, id.year, id.rollType, id.language, id.partNo);
    const r = this.db
      .prepare(
        `update download_job set ${sets.join(', ')}
         where state_cd = ? and ac_no = ? and year = ? and roll_type = ? and language = ? and part_no = ?`
      )
      .run(...vals);
    return r.changes;
  }

  bumpAttempt(id) {
    this.db
      .prepare(
        `update download_job set attempts = attempts + 1, updated_at = ?
         where state_cd = ? and ac_no = ? and year = ? and roll_type = ? and language = ? and part_no = ?`
      )
      .run(this._now(), id.stateCd, id.acNo, id.year, id.rollType, id.language, id.partNo);
  }

  // Progress for one AC+language+roll: how many parts and how many verified.
  acProgress(stateCd, acNo, year, rollType, language) {
    return this.db
      .prepare(
        `select count(*) total, sum(status='verified') verified, sum(status='failed') failed
         from download_job
         where state_cd=? and ac_no=? and year=? and roll_type=? and language=?`
      )
      .get(stateCd, acNo, year, rollType, language);
  }

  verifiedPartNos(stateCd, acNo, year, rollType, language) {
    return this.db
      .prepare(
        `select part_no from download_job
         where state_cd=? and ac_no=? and year=? and roll_type=? and language=? and status='verified'`
      )
      .all(stateCd, acNo, year, rollType, language)
      .map((r) => r.part_no);
  }

  logCrawl(scope, status, count, error) {
    this.db
      .prepare(
        `insert into crawl_log (scope, status, count, error, updated_at)
         values (?, ?, ?, ?, ?)
         on conflict(scope) do update set
           status = excluded.status, count = excluded.count,
           error = excluded.error, updated_at = excluded.updated_at`
      )
      .run(scope, status, count ?? null, error ?? null, this._now());
  }

  stats() {
    const one = (sql) => this.db.prepare(sql).get();
    const all = (sql) => this.db.prepare(sql).all();
    return {
      states: one('select count(*) c from state').c,
      districts: one('select count(*) c from district').c,
      acs: one('select count(*) c from ac').c,
      jobs: one('select count(*) c from download_job').c,
      crawlErrors: all(`select scope, error from crawl_log where status = 'error'`),
      perState: all(
        `select s.state_cd, s.state_name,
                (select count(*) from district d where d.state_cd = s.state_cd) districts,
                (select count(*) from ac a where a.state_cd = s.state_cd) acs
         from state s order by s.state_cd`
      ),
    };
  }

  jobStats() {
    const all = (sql) => this.db.prepare(sql).all();
    return {
      total: this.db.prepare('select count(*) c from download_job').get().c,
      byStatus: all('select status, count(*) c from download_job group by status'),
      byGroup: all(
        `select state_cd, year, roll_type, language, count(*) parts
         from download_job
         group by state_cd, year, roll_type, language
         order by state_cd, year desc, roll_type, language`
      ),
    };
  }
}

module.exports = { Manifest, pick };
