"""The DJ mixer: fetches a requested track, beat-analyzes it, and mixes it into the live stream.

Mix() runs on AudioPacer's wall-clock tick (media/pacer.py), never on receiver delivery - a deck
wired to receiver callbacks would freeze exactly when the live source goes quiet, which is the one
case this feature exists to paper over. Everything except the per-tick gain blend (fetching,
decoding, beat analysis) happens ahead of time on background tasks.
"""
