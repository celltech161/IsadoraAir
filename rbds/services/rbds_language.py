# RDS/RBDS Language Identification Code (LIC) table -- transmitted via
# UECP MEC 0x1A (Slow Labeling Codes), variant 3. The primary UECP spec
# (EBU-SPB 490 v5.1 section 3.3.12) defines the generic slow-labelling
# wire format but explicitly defers per-variant field MEANINGS to the
# underlying RDS standard -- the same situation already documented for
# ECC (variant 0) in rbds/services/uecp.py's mec_ecc(). The variant-3
# assignment and this code table are corroborated against redsea's own
# group-1A decoder (src/station.cc:390-391, src/tables.cc:107-145,
# `getLanguageString`), extracted programmatically (not hand-transcribed)
# to avoid an off-by-one -- see
# scratchpad/rbds_bench/lic_group1a_rtplus/00_LIC_SOURCE_MAP.md for the
# full derivation. This is the standard EN 50067/IEC 62106-2 Annex J
# RDS language codes list, reproduced consistently across the RDS
# ecosystem, not a redsea-specific invention.
#
# Code 0 ("Unknown") is itself a defined table entry, not an absence --
# RBDSConfig.language_code being blank/None is a DIFFERENT, third state
# meaning "IsadoraAir does not send LIC at all" (see that field's own
# help text). Unassigned numeric codes (e.g. 44-63, 65-68) are omitted
# from this list entirely so an operator can never select one through
# the admin UI.
RBDS_LANGUAGE_CHOICES = [
    (0, "Unknown"), (1, "Albanian"), (2, "Breton"), (3, "Catalan"),
    (4, "Croatian"), (5, "Welsh"), (6, "Czech"), (7, "Danish"),
    (8, "German"), (9, "English"), (10, "Spanish"), (11, "Esperanto"),
    (12, "Estonian"), (13, "Basque"), (14, "Faroese"), (15, "French"),
    (16, "Frisian"), (17, "Irish"), (18, "Gaelic"), (19, "Galician"),
    (20, "Icelandic"), (21, "Italian"), (22, "Lappish"), (23, "Latin"),
    (24, "Latvian"), (25, "Luxembourgian"), (26, "Lithuanian"), (27, "Hungarian"),
    (28, "Maltese"), (29, "Dutch"), (30, "Norwegian"), (31, "Occitan"),
    (32, "Polish"), (33, "Portuguese"), (34, "Romanian"), (35, "Romansh"),
    (36, "Serbian"), (37, "Slovak"), (38, "Slovene"), (39, "Finnish"),
    (40, "Swedish"), (41, "Turkish"), (42, "Flemish"), (43, "Walloon"),
    (64, "Background"),
    (69, "Zulu"), (70, "Vietnamese"), (71, "Uzbek"),
    (72, "Urdu"), (73, "Ukrainian"), (74, "Thai"), (75, "Telugu"),
    (76, "Tatar"), (77, "Tamil"), (78, "Tadzhik"), (79, "Swahili"),
    (80, "SrananTongo"), (81, "Somali"), (82, "Sinhalese"), (83, "Shona"),
    (84, "Serbo-Croat"), (85, "Ruthenian"), (86, "Russian"), (87, "Quechua"),
    (88, "Pushtu"), (89, "Punjabi"), (90, "Persian"), (91, "Papamiento"),
    (92, "Oriya"), (93, "Nepali"), (94, "Ndebele"), (95, "Marathi"),
    (96, "Moldovian"), (97, "Malaysian"), (98, "Malagasay"), (99, "Macedonian"),
    (100, "Laotian"), (101, "Korean"), (102, "Khmer"), (103, "Kazakh"),
    (104, "Kannada"), (105, "Japanese"), (106, "Indonesian"), (107, "Hindi"),
    (108, "Hebrew"), (109, "Hausa"), (110, "Gurani"), (111, "Gujurati"),
    (112, "Greek"), (113, "Georgian"), (114, "Fulani"), (115, "Dari"),
    (116, "Churash"), (117, "Chinese"), (118, "Burmese"), (119, "Bulgarian"),
    (120, "Bengali"), (121, "Belorussian"), (122, "Bambora"), (123, "Azerbaijan"),
    (124, "Assamese"), (125, "Armenian"), (126, "Arabic"), (127, "Amharic"),
]

# RBDSConfig.language_code's own choices -- prepends the "disabled"
# sentinel (None), same pattern as rbds_pty.py's
# CATEGORY_PTY_OVERRIDE_CHOICES.
RBDS_LANGUAGE_CONFIG_CHOICES = [(None, "Disabled / not transmitted")] + RBDS_LANGUAGE_CHOICES
