"""Output JSON schemas + instructions for each page kind.

This is the domain knowledge (what fields a roll has, how change is encoded);
the extractor stays generic. Schemas are intentionally loose on enums — we
normalize/validate in assemble.py rather than risk the model refusing to answer.
"""

CARD_PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "section_header": {"type": "string"},
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "serial_no": {"type": "integer"},
                    "marker_box": {"type": "string"},
                    "epic_no": {"type": "string"},
                    "full_name": {"type": "string"},
                    "relation_type": {"type": "string"},
                    "relation_name": {"type": "string"},
                    "house_number": {"type": "string"},
                    "age": {"type": "integer"},
                    "gender": {"type": "string"},
                    "deleted_watermark": {"type": "boolean"},
                    "deletion_reason_code": {"type": "string"},
                },
                "required": ["serial_no", "full_name"],
            },
        },
    },
    "required": ["section_header", "cards"],
}

CARD_PAGE_INSTRUCTION = (
    "This is a strip from an Indian electoral roll. Transcribe EVERY voter card you can read.\n"
    "Each card has: a small marker box + a boxed serial number (top-left), an EPIC code "
    "(top-right), Name, a relation line, House Number, Age, Gender, and a photo box.\n"
    "- epic_no: the alphanumeric ID in the TOP-RIGHT of the card (3 letters + 7 digits, e.g. WKH4726410). "
    "It is present on almost every card — read it carefully; only use '' if truly absent/illegible.\n"
    "- relation_type: classify the relation line as exactly one of 'Father', 'Husband', or 'Mother' "
    "based on whether it reads \"Father's Name\", \"Husband's Name\", or \"Mother's Name\". "
    "Put the person's name in relation_name.\n"
    "- section_header: the exact header text at the top of this strip (e.g. 'Section No and Name 1-Block 1' "
    "or 'List of Additions 1 (..)'). Empty string if none is visible.\n"
    "- marker_box: exactly what is in the small box next to the serial (a letter, a '#', a number, or '').\n"
    "- deleted_watermark: true only if a diagonal DELETED stamp crosses the card; then put the "
    "reason letter (E/S/R/M/Q) in deletion_reason_code.\n"
    "Transcribe text as printed. Do not invent cards; include only cards whose fields are legible."
)

COVER_SCHEMA = {
    "type": "object",
    "properties": {
        "state_name": {"type": "string"},
        "year_of_revision": {"type": "integer"},
        "qualifying_date": {"type": "string"},
        "type_of_revision": {"type": "string"},
        "date_of_publication": {"type": "string"},
        "date_of_updation": {"type": "string"},
        "roll_identification": {"type": "string"},
        "ac_no": {"type": "integer"},
        "ac_name": {"type": "string"},
        "ac_reservation": {"type": "string"},
        "pc_no": {"type": "integer"},
        "pc_name": {"type": "string"},
        "part_no": {"type": "integer"},
        "district_name": {"type": "string"},
        "main_town_or_village": {"type": "string"},
        "post_office": {"type": "string"},
        "police_station": {"type": "string"},
        "tehsil_mandal": {"type": "string"},
        "pin_code": {"type": "string"},
        "polling_station_no": {"type": "integer"},
        "polling_station_name": {"type": "string"},
        "polling_station_address": {"type": "string"},
        "polling_station_type": {"type": "string"},
        "num_auxiliary_stations": {"type": "integer"},
        "sections": {"type": "array", "items": {"type": "object", "properties": {
            "section_no": {"type": "integer"}, "section_name": {"type": "string"}}}},
        "starting_serial_no": {"type": "integer"},
        "ending_serial_no": {"type": "integer"},
        "net_male": {"type": "integer"},
        "net_female": {"type": "integer"},
        "net_third_gender": {"type": "integer"},
        "net_total": {"type": "integer"},
    },
}

COVER_INSTRUCTION = (
    "This is the cover page of an Indian electoral roll. Extract: the state name (from the title); "
    "the revision year, qualifying date, type of revision, date of publication/updation, and Roll "
    "Identification text; the Assembly Constituency number/name/reservation and Parliamentary "
    "Constituency number/name; the Part number; the area details (district, main town/village, post "
    "office, police station, tehsil/mandal, pin code); the polling-station number/name/address/type "
    "and number of auxiliary stations; the list of sections (number + name from 'No. and name of "
    "sections in the part'); and the 'Number of Electors' table (starting/ending serial, "
    "Male/Female/Third Gender/Total). Dates as printed (DD-MM-YYYY)."
)

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "net_male": {"type": "integer"},
        "net_female": {"type": "integer"},
        "net_third_gender": {"type": "integer"},
        "net_total": {"type": "integer"},
        "additions_total": {"type": "integer"},
        "deletions_total": {"type": "integer"},
        "num_modifications": {"type": "integer"},
    },
}

SUMMARY_INSTRUCTION = (
    "This is the 'Summary of Electors' page (table A has rows I Mother Roll, II List of Additions, "
    "III List of Deletions, IV gender-modification difference, and a NET row).\n"
    "Read the NET row, labeled 'Net Elector in this Roll after this Revision (I+II-III+IV)' — it has "
    "four numbers: Male -> net_male, Female -> net_female, Third Gender -> net_third_gender, Total -> net_total. "
    "Read ALL FOUR.\n"
    "Also read the 'List of Additions' row Total -> additions_total, the 'List of Deletions' row Total -> "
    "deletions_total, and from table B the NUMBER OF MODIFICATIONS -> num_modifications."
)
