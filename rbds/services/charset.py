"""Shared RDS text normalization + G0 character-set encoding.

RDS's G0 character table (EN 50067:1998 / IEC 62106 Annex E) is NOT
Latin-1 and NOT UTF-8 -- dozens of byte values mean something different
in each. Concrete, verified examples (byte value -> G0 meaning vs.
Latin-1 meaning for that SAME byte):
    0x24: G0 "¤" (generic currency sign)   Latin-1 "$"
    0x80: G0 "á" (a-acute)                 Latin-1 control code (unassigned)
    0xAA: G0 "£" (pound sign)              Latin-1 "ª" (ordinal indicator)
    0xC9: G0 "Ù" (U-grave)                 Latin-1 "É" (E-acute)
(Corrected 2026-08-02: an earlier revision of this docstring cited
0xA0 and 0xC9 for the á/Ú examples above -- those were transcription
slips in the docstring text only, not in RDS_G0_DECODE itself or in
any shipped/tested byte value; encode_rds_g0() and its test suite
were always correct. 0xA0 is actually G0 "ª"; Ú is at 0xC8, Ù at
0xC9. Verified directly against RDS_G0_DECODE below before writing
this correction, not re-asserted from memory.)
Sending Python's Latin-1 byte value for '$' (0x24) is received by a
real RDS decoder as a currency symbol, not a dollar sign -- this isn't
a cosmetic difference, it's a different glyph on the same wire byte.

Table transcribed from EN 50067:1998 Annex E via redsea
(https://github.com/windytan/redsea, src/text/rdsstring.cc, ISC
license), an independent, working, community-verified RDS decoder --
used here as the mirror (char -> byte) of its own (byte -> char)
decode table, since no free-access copy of the primary IEC 62106
Annex E text itself was available to transcribe directly.

Both mec_rt's RT+ offsets and RDS receivers alike see whatever this
module actually outputs, not Python's internal Unicode code points --
so normalize_text() must run BEFORE any length-based truncation or
RT+ start/length-marker arithmetic (it can change string length via
multi-char replacements like the ellipsis), while encode_rds_g0() is
strictly length-preserving (one output byte per input character) so
it's always safe to run AFTER those are finalized.

normalize_text() also NFC-canonicalizes its input first, before any of
the above (2026-08-05 fix). A canonically-equivalent decomposed
sequence (e.g. base "o" U+006F + combining diaeresis U+0308) is NOT
the same Python string as its precomposed form (U+00F6 "o with
diaeresis") even though Unicode treats them as the same character and
they render identically -- without NFC first, the base letter would
encode fine but the orphaned combining mark has no G0 representation
of its own and would fall back to a space, silently losing the accent
for that input form only. NFC (not NFKC/NFKD) specifically: it composes
canonically-equivalent sequences into their precomposed form without
the lossy compatibility folding NFKC/NFKD also apply (e.g. collapsing
ligatures or width variants) -- this project's own G0 table already
has real single-code-point slots for the composed forms this fixes, so
plain canonical composition is sufficient and doesn't reach for
anything broader than the actual problem."""

import unicodedata

# 256-entry RDS G0 DECODE table (byte -> char), EN 50067:1998 Annex E.
RDS_G0_DECODE = (
    " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", "\n", " ", " ", "\r", " ", " ",
    " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", " ", "­",
    " ", "!", "\"", "#", "¤", "%", "&", "'", "(", ")", "*", "+", ",", "-", ".", "/",
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", ":", ";", "<", "=", ">", "?",
    "@", "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O",
    "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z", "[", "\\", "]", "―", "_",
    "‖", "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m", "n", "o",
    "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z", "{", "|", "}", "¯", " ",
    "á", "à", "é", "è", "í", "ì", "ó", "ò", "ú", "ù", "Ñ", "Ç", "Ş", "β", "¡", "Ĳ",
    "â", "ä", "ê", "ë", "î", "ï", "ô", "ö", "û", "ü", "ñ", "ç", "ş", "ǧ", "ı", "ĳ",
    "ª", "α", "©", "‰", "Ǧ", "ě", "ň", "ő", "π", "€", "£", "$", "←", "↑", "→", "↓",
    "º", "¹", "²", "³", "±", "İ", "ń", "ű", "µ", "¿", "÷", "°", "¼", "½", "¾", "§",
    "Á", "À", "É", "È", "Í", "Ì", "Ó", "Ò", "Ú", "Ù", "Ř", "Č", "Š", "Ž", "Ð", "Ŀ",
    "Â", "Ä", "Ê", "Ë", "Î", "Ï", "Ô", "Ö", "Û", "Ü", "ř", "č", "š", "ž", "đ", "ŀ",
    "Ã", "Å", "Æ", "Œ", "ŷ", "Ý", "Õ", "Ø", "Þ", "Ŋ", "Ŕ", "Ć", "Ś", "Ź", "Ŧ", "ð",
    "ã", "å", "æ", "œ", "ŵ", "ý", "õ", "ø", "þ", "ŋ", "ŕ", "ć", "ś", "ź", "ŧ", " ",
)

# char -> byte, the mirror of RDS_G0_DECODE. Only walks 0x20..0xFF --
# codes 0x00..0x1F are control-code decode ARTIFACTS (several render
# as " " and one each as "\n"/"\r" in the table above purely for
# display purposes) and must never be treated as valid ENCODE targets
# for a literal space/newline/CR character; skipping them also means
# " " correctly binds to 0x20 (real space), not 0x00, on the setdefault
# walk below (first occurrence wins for characters -- like " " itself
# -- that appear at more than one code point).
_RDS_G0_ENCODE = {}
for _code in range(0x20, 0x100):
    _ch = RDS_G0_DECODE[_code]
    if len(_ch) == 1:
        _RDS_G0_ENCODE.setdefault(_ch, _code)
del _code, _ch

_CONTROL_TRANSLATION = {c: ord(" ") for c in range(0x00, 0x20)}
_CONTROL_TRANSLATION[0x7F] = ord(" ")

_SMART_PUNCTUATION = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "―": "-",
    "…": "...",
}


def normalize_text(text):
    """NFC-canonicalizes, then collapses smart quotes/dashes/ellipses
    to plain-ASCII equivalents, then replaces CR, LF, NUL, tab, and
    other C0/DEL control characters with a plain space. Must run
    BEFORE any length-based truncation or RT+ offset arithmetic -- both
    the NFC pass (a decomposed sequence collapsing to one precomposed
    character) and the ellipsis collapse ("…" -> "...") can change
    string length. Also closes the ASCII-transport command-injection
    path (an embedded newline surviving into a StereoTool ASCII RT=/
    PS= command could otherwise inject an extra command line -- see
    ascii_protocol.py)."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    for smart, plain in _SMART_PUNCTUATION.items():
        text = text.replace(smart, plain)
    return text.translate(_CONTROL_TRANSLATION)


def encode_rds_g0(text, replacement=" "):
    """Maps already-normalized text to real RDS G0 byte values -- NOT
    Latin-1, NOT UTF-8. Every character with no G0 representation
    becomes `replacement` (default space, matching mec_ps/mec_ptyn's
    existing non-conforming-byte convention) rather than being
    dropped -- dropping would shift every RT+ offset computed against
    this same string. Strictly length-preserving: one output byte per
    input character."""
    fallback = ord(replacement)
    out = bytearray(len(text))
    for i, ch in enumerate(text):
        out[i] = _RDS_G0_ENCODE.get(ch, fallback)
    return bytes(out)
