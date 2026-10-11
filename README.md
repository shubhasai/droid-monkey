# droid-monkey

A CLI agent that attacks an Android app with real-world faults (process death, rotation, network loss, revoked permissions), fixes every crash or lost state it finds in the app's source, and proves each fix by replaying the same attack.

It runs the whole loop without a human: plan, inject, observe, decide, diagnose, fix, verify, report.

Built for neatHack 2026 ("Build an agent that works (literally)").

- Sample app under attack: [shubhasai/MonkeyMart](https://github.com/shubhasai/MonkeyMart)
- Stack: Python 3.12, LangGraph, Claude (Haiku 5.5 by default, bring your own model), Claude Code as the coder, adb/emulator/Gradle, neatlogs tracing, Entire checkpoints

Status: work in progress.
