# NRSC-4-B RBDS Program Type table -- NOTE this is the US table, distinct
# from European RDS's PTY numbering (different meanings at several
# positions). Reconstructed from training knowledge/secondary research
# during planning, not transcribed byte-for-byte from the primary
# NRSC-4-B document -- cross-check against that document directly if any
# value here is ever in doubt.
RBDS_PTY_CHOICES = [
    (0, "No program type"), (1, "News"), (2, "Information"), (3, "Sports"),
    (4, "Talk"), (5, "Rock"), (6, "Classic Rock"), (7, "Adult Hits"),
    (8, "Soft Rock"), (9, "Top 40"), (10, "Country"), (11, "Oldies"),
    (12, "Soft"), (13, "Nostalgia"), (14, "Jazz"), (15, "Classical"),
    (16, "Rhythm and Blues"), (17, "Soft R&B"), (18, "Language"),
    (19, "Religious Music"), (20, "Religious Talk"), (21, "Personality"),
    (22, "Public"), (23, "College"), (24, "Spanish Talk"), (25, "Spanish Music"),
    (26, "Hip Hop"), (27, "Unassigned"), (28, "Unassigned"), (29, "Weather"),
    (30, "Emergency Test"), (31, "Emergency"),
]

# Used by Category.rbds_pty_override -- same table as RBDS_PTY_CHOICES
# above but with the numeric code folded into the label (per-category
# override needs the number visible so an operator picking "5" for a
# specific rotation isn't just reading a name), plus a leading
# "-- Default --" entry (value None) meaning "fall back to whatever
# RBDSConfig.pty is set to station-wide." None is a valid choice value
# here because the model field is nullable.
CATEGORY_PTY_OVERRIDE_CHOICES = [(None, "-- Default (use RBDS Config) --")] + [
    (code, f"{code} - {name}") for code, name in RBDS_PTY_CHOICES
]
