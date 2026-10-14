"""The ``assemble`` operation at the renderer boundary: a closed, bounded,
ordered request -- identities and descriptors only, never a path, command,
filter or parameter -- re-checked by the service against its own rules."""
import hashlib
import os
import uuid
from decimal import Decimal

from django.test import SimpleTestCase

from active_messages import models as domain
from production.services.render import executor, protocol, provenance
from production.services.render.engine import RenderError

SHA = "a" * 64


def message(kind="urgent", **overrides):
    entry = {"role": "message", "message": str(uuid.uuid4()), "kind": kind, "media": str(uuid.uuid4()),
             "sha256": SHA, "bytes": 1000, "duration_seconds": "1.000000"}
    entry.update(overrides)
    return entry


def stinger(**overrides):
    entry = {"role": "stinger", "key": "stinger", "reference": str(uuid.uuid4()), "media": str(uuid.uuid4()),
             "sha256": SHA, "bytes": 1000, "duration_seconds": "1.000000"}
    entry.update(overrides)
    return entry


def request(inputs, **overrides):
    """An assemble request whose expected digest matches its inputs (when they
    are well formed enough to have one)."""
    recipe = provenance.assembly("active_message_mix")
    try:
        digest = provenance.digest(provenance.assembly_document(
            recipe, [protocol.assembly_input_of(entry) for entry in inputs]))
    except (KeyError, TypeError):
        digest = "b" * 64
    body = {"v": 1, "op": "assemble", "request_id": "0" * 32, "correlation": {"execution": str(uuid.uuid4())},
            "recipe": {"key": recipe.key, "version": recipe.version}, "inputs": inputs,
            "expected_digest": digest, "timeout": 60}
    body.update(overrides)
    return body


def parse(body):
    return protocol.parse_assemble(body, max_bytes=10 ** 9)


class ShapeTests(SimpleTestCase):
    def test_the_ceilings_match_the_domains_absolute_ceilings(self):
        self.assertEqual(protocol.MAX_ASSEMBLE_MESSAGES, domain.ABSOLUTE_MAX_ACTIVE_MESSAGES)
        self.assertEqual(protocol.MAX_ASSEMBLE_INPUTS, domain.ABSOLUTE_MAX_ACTIVE_MESSAGES + 1)
        self.assertEqual(protocol.MAX_ASSEMBLE_TOTAL_SECONDS, domain.ABSOLUTE_MAX_AGGREGATE_SECONDS)
        self.assertGreaterEqual(protocol.MAX_FDS, protocol.MAX_ASSEMBLE_INPUTS)

    def test_24_messages_and_a_stinger_are_accepted_in_airing_order(self):
        inputs = [stinger()] + [message("urgent") for _ in range(12)] + [message("public_address") for _ in range(12)]
        parsed = parse(request(inputs))
        self.assertEqual([entry["role"] for entry in parsed["inputs"]], ["stinger"] + ["message"] * 24)

    def test_25_messages_are_refused_at_the_boundary(self):
        with self.assertRaises(protocol.ProtocolError):
            parse(request([message("public_address") for _ in range(25)]))
        with self.assertRaises(protocol.ProtocolError):
            parse(request([stinger()] + [message("urgent") for _ in range(25)]))      # 26 inputs

    def test_ordering_rules(self):
        cases = {
            "stinger not first": [message("urgent"), stinger()],
            "two stingers": [stinger(), stinger(), message("urgent")],
            "stinger ahead of public address only": [stinger(), message("public_address")],
            "public address before urgent": [message("public_address"), message("urgent")],
        }
        for label, inputs in cases.items():
            with self.subTest(label), self.assertRaises(protocol.ProtocolError) as caught:
                parse(request(inputs))
            self.assertEqual(caught.exception.code, "input_order")
        same = message("urgent")
        with self.assertRaises(protocol.ProtocolError) as caught:
            parse(request([same, dict(same, media=str(uuid.uuid4()))]))
        self.assertEqual(caught.exception.code, "input_order")

    def test_no_path_command_or_parameter_is_accepted(self):
        bad = {
            "a path on an input": [message("urgent", path="/srv/isadoraair/x.flac")],
            "a url as media": [message("urgent", media="file:///etc/passwd")],
            "an unknown role": [message("urgent", role="file")],
            "a filter on an input": [message("urgent", filter="volume=2")],
            "no messages": [stinger()],
            "empty": [],
            "an input that is not an object": ["/srv/a.flac"],
            "an uppercase hash": [message("urgent", sha256="A" * 64)],
            "zero bytes": [message("urgent", bytes=0)],
            "a negative duration": [message("urgent", duration_seconds="-1")],
            "an unknown kind": [message("weather")],
        }
        for label, inputs in bad.items():
            with self.subTest(label), self.assertRaises(protocol.ProtocolError):
                parse(request(inputs))
        for label, extra in {"params": {"params": {"gap_ms": 5000}}, "source": {"source": {}},
                             "references": {"references": {}}, "command": {"command": "ffmpeg"}}.items():
            with self.subTest(label), self.assertRaises(protocol.ProtocolError):
                parse({**request([message("urgent")]), **extra})

    def test_bounded_duration(self):
        with self.assertRaises(protocol.ProtocolError):
            parse(request([message("urgent", duration_seconds="1200.000001")]))
        with self.assertRaises(protocol.ProtocolError):
            parse(request([message("urgent", duration_seconds="700"), message("public_address",
                                                                             duration_seconds="501")]))
        parse(request([message("urgent", duration_seconds="700"), message("public_address", duration_seconds="500")]))

    def test_the_render_operation_is_unchanged(self):
        self.assertEqual(protocol.operation_of({"op": "render"}), "render")
        self.assertEqual(protocol.operation_of({"op": "anything"}), "render")     # refused by the render parser
        with self.assertRaises(protocol.ProtocolError):
            protocol.parse_request(request([message("urgent")]), max_bytes=10 ** 9, max_seconds=Decimal(3600))


class PlanTests(SimpleTestCase):
    def test_the_recipe_and_its_provenance(self):
        inputs = [stinger(), message("urgent"), message("public_address")]
        recipe, digest, document = executor.plan_assembly(parse(request(inputs)))
        self.assertEqual((recipe.key, recipe.version, document["assembly"], document["engine"]),
                         ("active_message_mix", 1, "active_message_mix", provenance.ENGINE_VERSION))
        self.assertEqual(document["params"], {"join": "sequential", "gap_ms": 0, "sample_rate": 48000, "channels": 2,
                                              "sample_fmt": "s16", "codec": "flac"})
        self.assertEqual([entry["role"] for entry in document["inputs"]], ["stinger", "message", "message"])
        self.assertEqual(set(document["inputs"][0]), {"role", "key", "reference", "media", "sha256"})
        self.assertEqual(set(document["inputs"][1]), {"role", "message", "kind", "media", "sha256"})

    def test_unknown_assembly_version_or_provenance_is_refused(self):
        inputs = [message("urgent")]
        for overrides, code in (({"recipe": {"key": "lcl_psa", "version": 1}}, "unknown_recipe"),
                                ({"recipe": {"key": "active_message_mix", "version": 2}}, "recipe_version_mismatch"),
                                ({"expected_digest": "c" * 64}, "provenance_mismatch")):
            with self.subTest(code), self.assertRaises(RenderError) as caught:
                executor.plan_assembly(parse(request(inputs, **overrides)))
            self.assertEqual(caught.exception.code, code)

    def test_every_input_identity_changes_the_digest(self):
        base = [stinger(), message("urgent"), message("public_address")]
        digest = executor.plan_assembly(parse(request(base)))[1]
        variants = [[dict(base[0], reference=str(uuid.uuid4())), *base[1:]],
                    [base[0], dict(base[1], sha256="d" * 64), base[2]],
                    [base[0], dict(base[1], media=str(uuid.uuid4())), base[2]],
                    [base[0], dict(base[1], message=str(uuid.uuid4())), base[2]],
                    base[1:]]
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(executor.plan_assembly(parse(request(variant)))[1], digest)


class DescriptorTests(SimpleTestCase):
    """The executor snapshots every descriptor and refuses anything other than
    the claimed bytes -- before any step runs."""

    def write(self, data):
        fd = os.memfd_create("t")
        os.write(fd, data)
        reopened = os.open(f"/proc/self/fd/{fd}", os.O_RDONLY)
        os.close(fd)
        self.addCleanup(os.close, reopened)
        return reopened

    def run_assembly(self, inputs, fds):
        steps = []
        return executor.execute_assembly(parse(request(inputs)), fds, deadline=10 ** 12, ffmpeg="/bin/false",
                                         run_step=lambda argv, fds_, t: steps.append(argv) or {"status": "ok"}), steps

    def test_a_descriptor_count_mismatch_is_refused(self):
        data = b"x" * 1000
        inputs = [message("urgent", sha256=hashlib.sha256(data).hexdigest())]
        with self.assertRaises(RenderError) as caught:
            self.run_assembly(inputs, [])
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_bytes_other_than_the_claimed_ones_are_refused_before_any_step(self):
        data = b"x" * 1000
        steps = []
        inputs = [message("urgent", sha256=hashlib.sha256(b"y" * 1000).hexdigest())]
        with self.assertRaises(RenderError) as caught:
            executor.execute_assembly(parse(request(inputs)), [self.write(data)], deadline=10 ** 12,
                                      ffmpeg="/bin/false", run_step=lambda *a: steps.append(a) or {"status": "ok"})
        self.assertEqual(caught.exception.code, "descriptor_mismatch")
        self.assertEqual(steps, [])

    def test_steps_are_built_from_descriptors_and_fixed_arguments_only(self):
        data = b"x" * 1000
        sha = hashlib.sha256(data).hexdigest()
        inputs = [stinger(sha256=sha), message("urgent", sha256=sha), message("public_address", sha256=sha)]
        calls = []

        def fake_step(argv, pass_fds, timeout):
            calls.append(argv)
            out = int(argv[argv.index("fd:", argv.index("-i") + 2) - 1])
            os.write(out, b"\0" * 8)
            return {"status": "ok"}
        rendered = executor.execute_assembly(parse(request(inputs)), [self.write(data) for _ in inputs],
                                             deadline=10 ** 12, ffmpeg="/usr/bin/ffmpeg", run_step=fake_step)
        os.close(rendered.fd)
        self.assertEqual(len(calls), 4)                         # three single-input decodes, one encode
        for argv in calls:
            text = " ".join(argv)
            self.assertNotIn("/srv", text)
            self.assertEqual(argv.count("-i"), 1)               # never more than one input per step
            self.assertIn("-protocol_whitelist", argv)
            for value in argv:
                self.assertFalse(value.startswith("/") and value != "/usr/bin/ffmpeg", value)
