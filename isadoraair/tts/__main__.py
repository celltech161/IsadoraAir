"""Temporary module entry point for staged Kokoro validation.

The future installed `/usr/local/bin/isadoraair-tts` command will call the
shared TTS service. Foundation A deliberately does not migrate callers.
"""

from isadoraair.tts.kokoro import main


if __name__ == "__main__":
    raise SystemExit(main())
