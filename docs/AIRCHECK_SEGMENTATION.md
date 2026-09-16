# Aircheck active-session segmentation

One logical Aircheck session may contain any number of internal source
segments. Start and Stop remain the caller-facing boundary, and successful
Stop still produces exactly one final file.

## Proven Liquidsoap cut boundary

The cut ordering is intentionally strict:

1. Hold `/run/isadoraair/aircheck.lock`.
2. Atomically rename `aircheck-current.audio` to a unique handoff pathname on
   the same `/run` filesystem while Liquidsoap still owns the open inode.
3. Send `aircheck.reopen`.
4. Liquidsoap closes the renamed inode and opens a new
   `aircheck-current.audio`.
5. Copy the closed handoff to persistent staging, fsync it, atomically publish
   the committed segment name, and only then unlink the `/run` handoff.

This was tested outside production with installed Liquidsoap `2.4.0+dev` and
a deterministic 997 Hz WAV source. The open renamed inode continued growing
between rename and reopen. Three repeated reopens produced distinct old and
new inodes, four valid decodable WAV files, and continuous boundary samples
without an obvious gap, duplicate interval, truncation, or file swap.

A forced control failure used an unused telnet port after rename. With no new
fixed path present, renaming the handoff back restored the original inode; it
continued growing under the fixed pathname. If the command was processed but
only its response was lost, the appearance of a new fixed path distinguishes
that state: the closed handoff is retained for recovery rather than restored
over the new writer.

Do not simplify this to “reopen, then move the fixed path.” Once reopen has
created the next file, that ordering can move the new writer instead of the
segment that was meant to close.

## Persistent layout and recovery

Segments live beside the session's immutable intended destination:

```text
<destination-parent>/.isadoraair-aircheck-staging/<session-id>/
  segment-000001.<source-extension>
  segment-000002.<source-extension>
```

Sequence and ownership are discoverable from deterministic filesystem names;
they are not held only in web-process memory. A transfer first writes a hidden
`.partial`, flushes and fsyncs it, and atomically renames it to the committed
name. The `/run` handoff is deleted only after that commit. A crash therefore
leaves either the complete handoff, the complete committed segment, or both.
Retries ignore partial files and size-match an already committed segment
before removing a redundant handoff.

The maintenance timer observes the working file once a minute. The 64 MiB
value is therefore a cut trigger, not a mathematical maximum; up to one timer
interval of growth above it is expected.

## Stop and finalization

With earlier segments, Stop cuts and stages the final source, marks the row
`finalization in progress`, releases the Aircheck lock, and launches the
existing-style daemon worker. A subsequent logical recording can start while
that worker runs. All essential source state is already on persistent storage,
so loss of the daemon thread leaves manually recoverable sources and the
pending row; automatic restart/retry policy belongs to roadmap item 1.13B.

Finalization uses ffmpeg's concat demuxer. HE-AAC source is ADTS and is
stream-copied into M4A with `aac_adtstoasc`; MP3 is stream-copied; WAV PCM is
stream-copied through one WAV mux so the result has one correct header. FLAC
is losslessly decoded and re-encoded: ffmpeg stream-copy leaves the first
segment's STREAMINFO total-sample count in the final header, which makes the
container report a truncated duration. Successful output must pass ffprobe
with an audio stream and positive duration before atomic publication and
source cleanup.

On concat, validation, or publication failure, committed segments and their
staging directory remain in place. Ordinary short MP3/FLAC/WAV direct moves
and short HE-AAC ADTS-to-M4A remuxes retain their existing fast paths.
