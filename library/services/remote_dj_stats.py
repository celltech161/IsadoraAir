"""Bounded, privacy-safe Remote DJ WebRTC statistics helpers.

Only documented ``webrtcbin`` get-stats output and the small browser payload
defined here enter public engine state.  Raw SDP, ICE candidates, addresses,
ports and arbitrary RTCStats fields are deliberately never copied.
"""

import math
from collections.abc import Mapping


MAX_SAFE_COUNTER = (1 << 53) - 1
MAX_DURATION_MS = 60 * 60 * 1000.0
MAX_AUDIO_ENERGY = 1_000_000_000_000.0
CANDIDATE_TYPES = frozenset({"host", "srflx", "prflx", "relay"})
PROTOCOLS = frozenset({"udp", "tcp"})
ICE_STATES = frozenset({
    "new", "checking", "connected", "completed", "disconnected", "failed", "closed",
})
ICE_GATHERING_STATES = frozenset({"new", "gathering", "complete"})
DTLS_STATES = frozenset({"new", "connecting", "connected", "closed", "failed"})
DTLS_ROLES = frozenset({"client", "server", "unknown"})


def empty_selected_pair():
    return {
        "exists": False,
        "local_candidate_type": None,
        "remote_candidate_type": None,
        "protocol": None,
    }


def empty_transport_snapshot():
    return {
        "server": {
            "ice_state": None,
            "ice_gathering_state": None,
            "dtls_state": None,
            "dtls_role": None,
            "ice_transitions": [],
            "ice_gathering_transitions": [],
            "dtls_state_observations": [],
            "selected_pair": empty_selected_pair(),
        },
        "browser": {
            "ice_state": None,
            "rtt_ms": None,
            "selected_pair": empty_selected_pair(),
        },
    }


def empty_media_stats_snapshot():
    return {
        "remote_mic": {
            "packets_received": None,
            "packets_lost": None,
            "packets_discarded": None,
            "packets_repaired": None,
            "jitter_ms": None,
            "bytes_received": None,
        },
        "monitor_return": {
            "packets_sent": None,
            "bytes_sent": None,
            "remote_fraction_lost": None,
            "rtt_ms": None,
        },
        "browser_monitor": {
            "packets_received": None,
            "packets_lost": None,
            "jitter_ms": None,
            "concealed_samples": None,
            "concealment_events": None,
            "total_audio_energy": None,
        },
    }


def _mapping(value):
    if isinstance(value, Mapping):
        return dict(value)
    n_fields = getattr(value, "n_fields", None)
    nth_field_name = getattr(value, "nth_field_name", None)
    get_value = getattr(value, "get_value", None)
    if not callable(n_fields) or not callable(nth_field_name) or not callable(get_value):
        return None
    result = {}
    try:
        for index in range(n_fields()):
            name = nth_field_name(index)
            result[name] = get_value(name)
    except (TypeError, ValueError, RuntimeError):
        return None
    return result


def _field(record, *names):
    for name in names:
        if name in record:
            return record[name]
    return None


def _nick(value):
    if value is None:
        return None
    nick = getattr(value, "value_nick", None)
    if isinstance(nick, str):
        return nick.lower()
    if isinstance(value, str):
        return value.strip().lower().replace("_", "-")
    return None


def _stats_type(record):
    value = _nick(_field(record, "type"))
    aliases = {
        "inbound-rtp": "inbound-rtp",
        "outbound-rtp": "outbound-rtp",
        "remote-inbound-rtp": "remote-inbound-rtp",
        "transport": "transport",
        "candidate-pair": "candidate-pair",
        "local-candidate": "local-candidate",
        "remote-candidate": "remote-candidate",
    }
    return aliases.get(value)


def _finite_number(value, *, minimum=0.0, maximum=MAX_DURATION_MS):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return None
    return number


def _counter(value, *, signed=False):
    minimum = -MAX_SAFE_COUNTER if signed else 0
    number = _finite_number(value, minimum=minimum, maximum=MAX_SAFE_COUNTER)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _seconds_to_ms(value):
    seconds = _finite_number(value, maximum=MAX_DURATION_MS / 1000.0)
    return None if seconds is None else round(seconds * 1000.0, 3)


def _milliseconds(value):
    number = _finite_number(value)
    return None if number is None else round(number, 3)


def sanitize_browser_elapsed_ms(value):
    return _milliseconds(value)


def _fraction(value):
    number = _finite_number(value, maximum=1.0)
    return None if number is None else round(number, 6)


def _candidate_type(value):
    nick = _nick(value)
    return nick if nick in CANDIDATE_TYPES else None


def _protocol(value):
    nick = _nick(value)
    return nick if nick in PROTOCOLS else None


def _record_id(record, fallback):
    value = _field(record, "id")
    return value if isinstance(value, str) and 0 < len(value) <= 256 else fallback


def _records(report):
    outer = _mapping(report)
    if outer is None:
        return []
    records = []
    for fallback, value in outer.items():
        record = _mapping(value)
        if record is None or _stats_type(record) is None:
            continue
        records.append((_record_id(record, str(fallback)), record))
    return records


def _media_record(records, stats_type):
    candidates = [(record_id, record) for record_id, record in records
                  if _stats_type(record) == stats_type]
    audio = [item for item in candidates
             if _nick(_field(item[1], "kind", "media-type", "media_type")) == "audio"]
    pool = audio or (candidates if len(candidates) == 1 else [])
    return sorted(pool, key=lambda item: item[0])[0] if pool else (None, None)


def _linked_remote_inbound(records, outbound_id):
    candidates = [(record_id, record) for record_id, record in records
                  if _stats_type(record) == "remote-inbound-rtp"]
    linked = [item for item in candidates
              if outbound_id is not None and _field(item[1], "local-id", "local_id") == outbound_id]
    pool = linked or (candidates if len(candidates) == 1 else [])
    return sorted(pool, key=lambda item: item[0])[0][1] if pool else None


def _audio_transport(records, inbound, outbound):
    transport_ids = {
        value for record in (inbound, outbound) if record is not None
        for value in [_field(record, "transport-id", "transport_id")]
        if isinstance(value, str) and value
    }
    transports = {record_id: record for record_id, record in records
                  if _stats_type(record) == "transport"}
    if len(transport_ids) == 1:
        return transports.get(next(iter(transport_ids)))
    if not transport_ids and len(transports) == 1:
        return next(iter(transports.values()))
    # Multiple audio transports cannot be represented truthfully by the
    # compact state shape. Refuse to pick an arbitrary one.
    return None


def _selected_pair(records, transport):
    result = empty_selected_pair()
    if transport is None:
        return result
    pair_id = _field(
        transport, "selected-candidate-pair-id", "selected_candidate_pair_id"
    )
    if not isinstance(pair_id, str) or not pair_id:
        return result
    by_id = {record_id: record for record_id, record in records}
    pair = by_id.get(pair_id)
    if pair is None or _stats_type(pair) != "candidate-pair":
        return result
    local = by_id.get(_field(pair, "local-candidate-id", "local_candidate_id"))
    remote = by_id.get(_field(pair, "remote-candidate-id", "remote_candidate_id"))
    result["exists"] = True
    if local is not None:
        result["local_candidate_type"] = _candidate_type(
            _field(local, "candidate-type", "candidate_type")
        )
    if remote is not None:
        result["remote_candidate_type"] = _candidate_type(
            _field(remote, "candidate-type", "candidate_type")
        )
    protocols = {
        protocol for candidate in (local, remote) if candidate is not None
        for protocol in [_protocol(_field(candidate, "protocol"))]
        if protocol is not None
    }
    result["protocol"] = next(iter(protocols)) if len(protocols) == 1 else None
    return result


def parse_webrtc_stats(report):
    """Extract one fixed, JSON-safe snapshot from a get-stats reply."""
    records = _records(report)
    inbound_id, inbound = _media_record(records, "inbound-rtp")
    outbound_id, outbound = _media_record(records, "outbound-rtp")
    del inbound_id
    remote_inbound = _linked_remote_inbound(records, outbound_id)
    transport = _audio_transport(records, inbound, outbound)

    dtls_state = _nick(_field(transport or {}, "dtls-state", "dtls_state"))
    if dtls_state not in DTLS_STATES:
        dtls_state = None
    dtls_role = _nick(_field(transport or {}, "dtls-role", "dtls_role"))
    if dtls_role not in DTLS_ROLES:
        dtls_role = None

    return {
        "transport": {
            "dtls_state": dtls_state,
            "dtls_role": dtls_role,
            "selected_pair": _selected_pair(records, transport),
        },
        "remote_mic": {
            "packets_received": _counter(_field(inbound or {}, "packets-received", "packets_received")),
            "packets_lost": _counter(_field(inbound or {}, "packets-lost", "packets_lost"), signed=True),
            "packets_discarded": _counter(_field(inbound or {}, "packets-discarded", "packets_discarded")),
            "packets_repaired": _counter(_field(inbound or {}, "packets-repaired", "packets_repaired")),
            "jitter_ms": _seconds_to_ms(_field(inbound or {}, "jitter")),
            "bytes_received": _counter(_field(inbound or {}, "bytes-received", "bytes_received")),
        },
        "monitor_return": {
            "packets_sent": _counter(_field(outbound or {}, "packets-sent", "packets_sent")),
            "bytes_sent": _counter(_field(outbound or {}, "bytes-sent", "bytes_sent")),
            "remote_fraction_lost": _fraction(_field(remote_inbound or {}, "fraction-lost", "fraction_lost")),
            "rtt_ms": _seconds_to_ms(_field(remote_inbound or {}, "round-trip-time", "round_trip_time")),
        },
    }


def sanitize_browser_stats_payload(payload):
    """Allow-list one browser snapshot; silently discard every other field."""
    if not isinstance(payload, Mapping):
        return None
    inbound = payload.get("inbound")
    pair = payload.get("selected_pair")
    inbound = inbound if isinstance(inbound, Mapping) else {}
    pair = pair if isinstance(pair, Mapping) else {}
    ice_state = _nick(payload.get("ice_state"))
    if ice_state not in ICE_STATES:
        ice_state = None
    exists = pair.get("exists") is True
    selected_pair = empty_selected_pair()
    selected_pair.update({
        "exists": exists,
        "local_candidate_type": _candidate_type(pair.get("local_candidate_type")) if exists else None,
        "remote_candidate_type": _candidate_type(pair.get("remote_candidate_type")) if exists else None,
        "protocol": _protocol(pair.get("protocol")) if exists else None,
    })
    energy = _finite_number(
        inbound.get("total_audio_energy"), maximum=MAX_AUDIO_ENERGY
    )
    return {
        "ice_state": ice_state,
        "rtt_ms": _milliseconds(payload.get("rtt_ms")),
        "selected_pair": selected_pair,
        "inbound": {
            "packets_received": _counter(inbound.get("packets_received")),
            "packets_lost": _counter(inbound.get("packets_lost"), signed=True),
            "jitter_ms": _milliseconds(inbound.get("jitter_ms")),
            "concealed_samples": _counter(inbound.get("concealed_samples")),
            "concealment_events": _counter(inbound.get("concealment_events")),
            "total_audio_energy": None if energy is None else round(energy, 6),
        },
    }
