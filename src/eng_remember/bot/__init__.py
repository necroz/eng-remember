"""Telegram layer.

Kept free of imports on purpose: handlers reach into ``eng_remember.bot.texts``
and friends directly, and an eager re-export here would make that a circular
import.
"""
