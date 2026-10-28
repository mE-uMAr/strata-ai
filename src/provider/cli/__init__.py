"""Subscription-backed CLI inference: the default path before launch.

One module per adapter, and no adapter imports another. `_process` is the machinery
they share (spawning, timeouts, isolation), not an adapter.
"""
