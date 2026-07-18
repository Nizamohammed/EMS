"""Turn a RollResult into loadable SQL for db/schema.sql.

Emits a psql script (uses \\gset to thread surrogate ids) that inserts the
geography spine, the roll + supplement, every elector, the QC summary/cover_count,
and the Tier-1 identity (one person per EPIC, one placement per elector).

Load it with:  psql -d <db> -v ON_ERROR_STOP=1 -f <file>
(also runs unchanged inside the Docker harness we used for db/schema.sql).
"""
from __future__ import annotations
from .types import RollResult

# stateCd -> name fallback when the cover read doesn't supply it
STATE_NAMES = {
    "S01": "Andhra Pradesh", "S02": "Arunachal Pradesh", "S03": "Assam", "S04": "Bihar",
    "S05": "Goa", "S06": "Gujarat", "S07": "Haryana", "S08": "Himachal Pradesh",
    "S09": "Jammu and Kashmir", "S10": "Karnataka", "S11": "Kerala", "S12": "Madhya Pradesh",
    "S13": "Maharashtra", "S14": "Manipur", "S15": "Meghalaya", "S16": "Mizoram",
    "S17": "Nagaland", "S18": "Odisha", "S19": "Punjab", "S20": "Rajasthan",
    "S21": "Sikkim", "S22": "Tamil Nadu", "S23": "Tripura", "S24": "Uttar Pradesh",
    "S25": "West Bengal", "S26": "Chhattisgarh", "S27": "Jharkhand", "S28": "Uttarakhand",
    "S29": "Telangana", "U01": "Andaman and Nicobar Islands", "U02": "Chandigarh",
    "U03": "Dadra and Nagar Haveli and Daman and Diu", "U05": "Delhi", "U06": "Lakshadweep",
    "U07": "Puducherry", "U08": "Ladakh",
}


def _s(v):
    if v is None or v == "":
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


def _i(v):
    try:
        return str(int(v))
    except (TypeError, ValueError):
        return "NULL"


def _d(v):
    """DD-MM-YYYY -> 'YYYY-MM-DD'; pass through 'NULL' otherwise."""
    if not v:
        return "NULL"
    p = str(v).replace("/", "-").split("-")
    if len(p) == 3 and len(p[0]) == 2:
        return f"'{p[2]}-{p[1]}-{p[0]}'"
    if len(p) == 3 and len(p[0]) == 4:
        return f"'{p[0]}-{p[1]}-{p[2]}'"
    return "NULL"


def _resv(v):
    """Normalize reservation status to the schema enum (GEN/SC/ST)."""
    if not v:
        return "NULL"
    s = str(v).strip().upper()
    if s.startswith("GEN"):
        return "'GEN'"
    if s.startswith("SC") or "SCHEDULED CASTE" in s:
        return "'SC'"
    if s.startswith("ST") or "SCHEDULED TRIBE" in s:
        return "'ST'"
    return "NULL"


def _pstype(v):
    """Normalize polling-station type to the schema enum (General/Male/Female)."""
    s = (str(v).strip().lower() if v else "general")
    if s.startswith("male"):
        return "'Male'"
    if s.startswith("female"):
        return "'Female'"
    return "'General'"


def to_sql(result: RollResult) -> str:
    ctx, cover, summary, electors = result.context, result.cover, result.summary, result.electors
    state_cd = ctx.state_cd or "S00"
    state_name = cover.get("state_name") or STATE_NAMES.get(state_cd, state_cd)
    ac_no = ctx.ac_no or cover.get("ac_no")
    part_no = ctx.part_no or cover.get("part_no") or 1
    roll_type = ctx.roll_type or "FinalRoll"
    lang = ctx.language_code or "ENG"

    out = ["begin;"]
    a = out.append

    a(f"insert into state(state_cd, state_name) values({_s(state_cd)}, {_s(state_name)}) "
      f"on conflict (state_cd) do update set state_name = excluded.state_name returning state_id \\gset")
    # Geography spine is shared across every part/roll of an AC, so upsert (not plain
    # insert) — a state batch loads thousands of parts under the same district/PC/AC.
    # do-update-returning yields the surrogate id whether the row is new or already there.
    a(f"insert into district(state_id, district_name) values(:state_id, "
      f"{_s(cover.get('district_name') or 'UNKNOWN')}) "
      f"on conflict (state_id, district_name) do update set district_name = excluded.district_name "
      f"returning district_id \\gset")
    # PC is optional geography (ac.pc_id is nullable) and the cover read often misses it;
    # only create/link a PC when we have a valid pc_no, else leave ac.pc_id NULL rather
    # than fabricate pc_no=0 (which fails the pc_no>=1 check and would crash the roll).
    try:
        pc_no_val = int(cover.get("pc_no"))
    except (TypeError, ValueError):
        pc_no_val = None
    if pc_no_val and pc_no_val >= 1:
        a(f"insert into parliamentary_constituency(state_id, pc_no, pc_name) values(:state_id, "
          f"{pc_no_val}, {_s(cover.get('pc_name') or 'UNKNOWN')}) "
          f"on conflict (state_id, pc_no) do update set pc_name = excluded.pc_name returning pc_id \\gset")
        pc_ref = ":pc_id"
    else:
        pc_ref = "NULL"
    a(f"insert into assembly_constituency(state_id, district_id, pc_id, ac_no, ac_name, reservation_status) "
      f"values(:state_id, :district_id, {pc_ref}, {_i(ac_no or 0)}, "
      f"{_s(cover.get('ac_name') or f'AC {ac_no}')}, {_resv(cover.get('ac_reservation'))}) "
      f"on conflict (state_id, ac_no) do update set ac_name = excluded.ac_name returning ac_id \\gset")
    a(f"insert into part(ac_id, part_no, main_town_or_village, post_office, police_station, tehsil_mandal, "
      f"pin_code, polling_station_no, polling_station_name, polling_station_address, polling_station_type, "
      f"num_auxiliary_stations) values(:ac_id, {_i(part_no)}, {_s(cover.get('main_town_or_village'))}, "
      f"{_s(cover.get('post_office'))}, {_s(cover.get('police_station'))}, {_s(cover.get('tehsil_mandal'))}, "
      f"{_s(cover.get('pin_code'))}, {_i(cover.get('polling_station_no'))}, "
      f"{_s(cover.get('polling_station_name'))}, {_s(cover.get('polling_station_address'))}, "
      f"{_pstype(cover.get('polling_station_type'))}, "
      f"{_i(cover.get('num_auxiliary_stations') or 0)}) "
      f"on conflict (ac_id, part_no) do update set part_no = excluded.part_no returning part_id \\gset")

    # sections (from cover; default to one if none read)
    sections = cover.get("sections") or [{"section_no": 1, "section_name": None}]
    vals = ",".join(f"(:part_id, {_i(s.get('section_no'))}, {_s(s.get('section_name'))})"
                    for s in sections if s.get("section_no"))
    if vals:
        a(f"insert into section(part_id, section_no, section_name) values {vals} "
          f"on conflict (part_id, section_no) do nothing;")

    # era_id: the 2026 SIR rolls all use the 2008 Delimitation boundaries (SIR is a
    # revision, not a re-delimitation). Set the correct era here once we ingest rolls
    # published under a newer delimitation (see db/TEMPORAL_GEOGRAPHY.md).
    a(f"insert into roll(part_id, era_id, year_of_revision, qualifying_date, type_of_revision, date_of_publication, "
      f"date_of_updation, roll_identification, roll_type, revision_no, language_code, "
      f"source_pdf_filename, source_pdf_sha256) values(:part_id, "
      f"(select era_id from delimitation_era where era_name='2008 Delimitation'), "
      f"{_i(cover.get('year_of_revision') or 0)}, {_d(cover.get('qualifying_date'))}, "
      f"{_s(cover.get('type_of_revision'))}, {_d(cover.get('date_of_publication'))}, "
      f"{_d(cover.get('date_of_updation'))}, {_s(cover.get('roll_identification'))}, "
      f"{_s(roll_type)}, 1, {_s(lang)}, {_s(ctx.source_pdf_filename)}, "
      f"{_s(ctx.source_pdf_sha256)}) returning roll_id \\gset")

    has_supp = bool((summary.additions_total or 0) or (summary.deletions_total or 0)
                    or (summary.num_modifications or 0)
                    or any(e.status in ("added", "modified") for e in electors))
    if has_supp:
        a(f"insert into supplement(roll_id, supplement_no, type_of_revision) values(:roll_id, 1, "
          f"{_s(cover.get('type_of_revision'))}) returning supplement_id \\gset")

    # electors (multi-row)
    def sect_ref(e):
        return (f"(select section_id from section where part_id=:part_id and section_no={int(e.section_no)})"
                if e.section_no else "NULL")

    def supp_ref(e):
        return ":supplement_id" if (has_supp and e.status in ("added", "modified")) else "NULL"

    rows = []
    for e in electors:
        rows.append(
            f"(:roll_id, {sect_ref(e)}, {supp_ref(e)}, {int(e.serial_no)}, {_s(e.epic_no)}, "
            f"{_s(e.full_name or 'UNKNOWN')}, {_s(e.relation_type)}, {_s(e.relation_name)}, "
            f"{_s(e.house_number)}, {_i(e.age)}, {_s(e.gender)}, true, {_s(e.status or 'active')}, "
            f"{_s(e.deletion_reason_code)})")
    if rows:
        a("insert into elector(roll_id, section_id, supplement_id, serial_no, epic_no, full_name, "
          "relation_type, relation_name, house_number, age, gender, photo_present, entry_status, "
          "deletion_reason_code) values\n" + ",\n".join(rows) + ";")

    a(f"insert into summary(roll_id, net_male, net_female, net_third_gender, net_total, num_modifications) "
      f"values(:roll_id, {_i(summary.net_male)}, {_i(summary.net_female)}, {_i(summary.net_third_gender)}, "
      f"{_i(summary.net_total)}, {_i(summary.num_modifications)});")
    a(f"insert into cover_count(roll_id, starting_serial_no, ending_serial_no, net_male, net_female, "
      f"net_third_gender, net_total) values(:roll_id, {_i(cover.get('starting_serial_no'))}, "
      f"{_i(cover.get('ending_serial_no'))}, {_i(cover.get('net_male'))}, {_i(cover.get('net_female'))}, "
      f"{_i(cover.get('net_third_gender'))}, {_i(cover.get('net_total'))});")

    # Tier-1 identity (EPIC-keyed). person_epic holds the full EPIC set (multi-card /
    # Tier-2 merges); placement resolves THROUGH it, so a previously-seen, re-issued, or
    # merged EPIC still finds its one person — no duplicate person, no lost appearance.
    # 1. a new person for each EPIC not yet registered anywhere
    a("insert into person(epic_no, canonical_name) select distinct on (epic_no) epic_no, full_name "
      "from elector where roll_id=:roll_id and epic_no is not null "
      "and epic_no not in (select epic_no from person_epic) "
      "on conflict (epic_no) do nothing;")
    # 2. register this roll's EPICs in person_epic (each maps to exactly one person)
    a("insert into person_epic(person_id, epic_no, first_roll_id) "
      "select p.person_id, p.epic_no, :roll_id from person p "
      "where p.epic_no in (select epic_no from elector where roll_id=:roll_id and epic_no is not null) "
      "and p.epic_no not in (select epic_no from person_epic) "
      "on conflict (epic_no) do nothing;")
    # 3. one placement per appearance, resolved via person_epic
    a("insert into placement(person_id, elector_id, match_method, match_confidence) "
      "select pe.person_id, e.elector_id, 'epic', 1.000 from elector e "
      "join person_epic pe on pe.epic_no = e.epic_no "
      "where e.roll_id=:roll_id on conflict (elector_id) do nothing;")

    a("commit;")
    return "\n".join(out) + "\n"
